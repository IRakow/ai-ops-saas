"""
AI Ops Orchestrator — The Brain
Manages the full pipeline: gathering_info → planning → coding → testing → deploying.
Uses Claude Opus 4.6 tool-use loop for the Implementer agent.
Uses ConsensusEngine for multi-agent planning decisions.
"""

import os
import re
import json
import time
import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger("ai_ops.orchestrator")

MODEL = config.AGENT_MODEL
WORKSPACE_BASE = config.WORKSPACE_BASE or "/srv/agent-workspace"
ALLOWED_EXTENSIONS = {".py", ".html", ".js", ".css", ".sql", ".json", ".txt", ".md", ".yml", ".yaml"}
FORBIDDEN_PATTERNS = [".env", "credentials", "secret", "../", "..\\"]


class AIOpsOrchestrator:
    """
    The main orchestrator that drives the AI Ops pipeline.
    Polls Supabase for sessions needing work, then runs the appropriate agent phase.
    """

    def __init__(self, api_key=None):
        # API key optional — using Claude CLI with Pro plan OAuth tokens
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

        self.model = MODEL
        self.running = False
        self._thread = None

        # Lazy imports to avoid circular dependencies
        self._service = None
        self._notification_service = None
        self._consensus_engine = None

    @property
    def service(self):
        if self._service is None:
            from app.services.ai_ops_service import AIOpsService
            self._service = AIOpsService()
        return self._service

    @property
    def notification_service(self):
        if self._notification_service is None:
            from app.services.ai_ops_notification_service import AIOpsNotificationService
            self._notification_service = AIOpsNotificationService()
        return self._notification_service

    @property
    def consensus_engine(self):
        if self._consensus_engine is None:
            from app.self_healing.consensus_engine import ConsensusEngine
            self._consensus_engine = ConsensusEngine(api_key=self.api_key)
        return self._consensus_engine

    # =========================================================================
    # BACKGROUND POLLING
    # =========================================================================

    def start(self, poll_interval=10):
        """Start the orchestrator as a background polling loop."""
        self.running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            args=(poll_interval,),
            daemon=False,
        )
        self._thread.start()
        logger.info(f"Orchestrator started (polling every {poll_interval}s)")

    def stop(self):
        """Stop the orchestrator."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=30)
        logger.info("Orchestrator stopped")

    def _poll_loop(self, interval):
        """Main polling loop — check for sessions needing work."""
        while self.running:
            try:
                self._process_pending_sessions()
            except Exception as e:
                logger.error(f"Poll loop error: {e}", exc_info=True)
            time.sleep(interval)

    def _process_pending_sessions(self, tenant_id=None):
        """Find sessions that need agent attention and process them."""
        # Look for sessions in actionable states
        actionable_states = ["gathering_info", "planning", "coding", "testing", "deploying_staging"]

        for state in actionable_states:
            try:
                from app.supabase_client import get_supabase_client
                supabase = get_supabase_client()
                query = supabase.table("ai_ops_sessions") \
                    .select("*") \
                    .eq("status", state) \
                    .order("updated_at", desc=False) \
                    .limit(1)
                if tenant_id:
                    query = query.eq("tenant_id", tenant_id)
                result = query.execute()

                if result.data:
                    session = result.data[0]
                    self._handle_session(session)
            except Exception as e:
                logger.error(f"Error checking {state} sessions: {e}")

    # =========================================================================
    # SESSION HANDLING
    # =========================================================================

    def _handle_session(self, session):
        """Route a session to the appropriate handler based on status."""
        session_id = session["id"]
        status = session["status"]

        logger.info(f"Handling session {session_id} (status: {status})")

        try:
            if status == "gathering_info":
                self._handle_gathering_info(session)
            elif status == "planning":
                self._handle_planning(session)
            elif status == "coding":
                self._handle_coding(session)
            elif status == "testing":
                self._handle_testing(session)
            elif status == "deploying_staging":
                self._handle_deploying(session)
        except Exception as e:
            logger.error(f"Error handling session {session_id}: {e}", exc_info=True)
            self.service.update_session_status(session_id, "failed")
            self.service.add_message(
                session_id, "system", "System",
                f"Pipeline error: {str(e)}",
                message_type="error"
            )
            self.notification_service.notify_pipeline_failed(
                session.get("title", "Untitled"),
                session_id,
                str(e)
            )

    # =========================================================================
    # PHASE 1: GATHERING INFO (Clarifier Agent)
    # =========================================================================

    def _handle_gathering_info(self, session):
        """Run the Clarifier agent to ask simple questions."""
        session_id = session["id"]
        messages = self.service.get_messages(session_id)

        # Check if there's a new user message that needs a response
        if not messages:
            return  # No messages yet, wait

        last_msg = messages[-1]

        # Only respond to user messages (don't respond to our own agent messages)
        if last_msg["sender_type"] != "user":
            return

        # Build conversation history for Claude
        conversation = self._build_conversation(messages)

        from app.services.ai_ops_prompts import CLARIFIER_PROMPT
        response = self._call_claude(CLARIFIER_PROMPT, conversation)

        if not response:
            return

        try:
            parsed = self._parse_json_response(response)
        except Exception:
            parsed = {"questions": [response], "ready_to_proceed": False}

        # Auto-generate title from first user message if not set
        if not session.get("title"):
            first_user_msg = next(
                (m["content"] for m in messages if m["sender_type"] == "user"), ""
            )
            title = first_user_msg[:100] + ("..." if len(first_user_msg) > 100 else "")
            self.service.update_session(session_id, title=title)

        if parsed.get("ready_to_proceed"):
            # Summarize understanding and move to planning
            summary = parsed.get("understanding_so_far", "")
            self.service.add_message(
                session_id, "agent", "Clarifier",
                f"Got it! Here's what I understand:\n\n{summary}\n\nI'm now passing this to the planning team.",
                message_type="status_update",
                metadata={"agent_role": "clarifier", "phase": "complete"}
            )
            self.service.update_session_status(session_id, "planning")
        else:
            # Ask questions
            questions = parsed.get("questions", [])
            if questions:
                question_text = "Thanks! A few quick questions:\n\n"
                for i, q in enumerate(questions, 1):
                    question_text += f"{i}. {q}\n"

                self.service.add_message(
                    session_id, "agent", "Clarifier",
                    question_text,
                    message_type="question",
                    metadata={"agent_role": "clarifier"}
                )

    # =========================================================================
    # PHASE 2: PLANNING (Consensus Engine)
    # =========================================================================

    def _handle_planning(self, session):
        """Use ConsensusEngine to generate a task plan."""
        session_id = session["id"]
        mode = session["mode"]

        # Collect all user messages as context
        messages = self.service.get_messages(session_id)
        user_context = "\n".join(
            f"{m['sender_name']}: {m['content']}"
            for m in messages
            if m["sender_type"] in ("user", "agent")
        )

        self.service.add_message(
            session_id, "system", "System",
            "The planning team is analyzing your request. This may take a minute...",
            message_type="status_update"
        )

        # Use ConsensusEngine for multi-agent planning
        if mode == "bug_fix":
            consensus_result = self.consensus_engine.analyze_error(
                error_type="user_reported_bug",
                error_message=user_context,
                traceback_text="(User-reported — no traceback available)",
                source_context="(To be investigated by agents)",
                service="ai_ops",
                fingerprint=session_id[:12],
            )
        else:
            consensus_result = self.consensus_engine.plan_feature(
                feature_name=session.get("title", "New Feature"),
                feature_description=user_context,
                fingerprint=session_id[:12],
            )

        # Now generate user-friendly task plan using the Planner agent
        from app.services.ai_ops_prompts import PLANNER_PROMPT

        planner_context = (
            f"The analysis team has completed their review. Here's their consensus:\n\n"
            f"Consensus reached: {consensus_result.consensus_reached}\n"
            f"Confidence: {consensus_result.confidence:.0%}\n\n"
        )

        if mode == "bug_fix":
            planner_context += (
                f"Diagnosis: {consensus_result.final_diagnosis}\n"
                f"Proposed fix: {consensus_result.final_fix}\n"
                f"Severity: {consensus_result.severity}\n"
            )
        else:
            planner_context += (
                f"Architecture: {consensus_result.architecture}\n"
                f"Implementation plan: {consensus_result.implementation_plan}\n"
                f"Files to change: {consensus_result.files_to_change}\n"
                f"Risk level: {consensus_result.risk_level}\n"
            )

        planner_context += f"\nOriginal user request:\n{user_context}"

        plan_response = self._call_claude(
            PLANNER_PROMPT,
            [{"role": "user", "content": planner_context}]
        )

        if not plan_response:
            self.service.update_session_status(session_id, "failed")
            return

        try:
            plan = self._parse_json_response(plan_response)
        except Exception:
            plan = {
                "tasks": [{"title": "Implement the requested changes", "description": plan_response}],
                "summary": plan_response,
            }

        # Create tasks in database
        tasks = plan.get("tasks", [])
        if tasks:
            self.service.create_tasks(session_id, tasks)

        # Post the plan as a message
        summary = plan.get("summary", "Plan generated.")
        task_list = "\n".join(
            f"**Task {t.get('task_number', i+1)}:** {t['title']}"
            for i, t in enumerate(tasks)
        )

        self.service.add_message(
            session_id, "agent", "Planning Team",
            f"{summary}\n\nHere's the plan:\n\n{task_list}",
            message_type="plan",
            metadata={
                "consensus_reached": consensus_result.consensus_reached,
                "confidence": consensus_result.confidence,
                "risk_level": plan.get("risk_level", ""),
            }
        )

        self.service.update_session_status(session_id, "awaiting_approval")

        # Send notification
        app_url = config.APP_BASE_URL
        plan_url = f"{app_url}/ai-ops/session/{session_id}"
        self.notification_service.notify_plan_ready(
            session.get("title", "Untitled"),
            session_id,
            plan_url
        )

    # =========================================================================
    # PHASE 3: CODING (Implementer Agent with tool-use)
    # =========================================================================

    def _handle_coding(self, session):
        """Run the Implementer agent with sandboxed tools."""
        session_id = session["id"]
        mode = session["mode"]

        tasks = self.service.get_tasks(session_id)
        if not tasks:
            self.service.update_session_status(session_id, "failed")
            return

        self.notification_service.notify_coding_started(
            session.get("title", "Untitled"), session_id
        )

        # Set up workspace
        workspace = os.path.join(WORKSPACE_BASE, session_id)
        os.makedirs(workspace, exist_ok=True)

        from app.services.ai_ops_prompts import get_implementer_prompt
        system_prompt = get_implementer_prompt(mode)

        # Collect context
        messages = self.service.get_messages(session_id)
        user_context = "\n".join(
            f"{m['sender_name']}: {m['content']}" for m in messages
        )

        task_context = "\n".join(
            f"Task {t['task_number']}: {t['title']} — {t.get('description', '')}"
            for t in tasks
        )

        # Process each task
        for task in tasks:
            task_id = task["id"]
            self.service.update_task(task_id, status="in_progress")
            self.service.add_message(
                session_id, "system", "System",
                f"Working on Task {task['task_number']}: {task['title']}",
                message_type="status_update"
            )

            try:
                result = self._run_implementer(
                    system_prompt=system_prompt,
                    task=task,
                    user_context=user_context,
                    task_context=task_context,
                    workspace=workspace,
                )

                self.service.update_task(
                    task_id,
                    status="completed",
                    files_changed=result.get("files_changed", []),
                    test_results=result.get("test_results", {}),
                )
            except Exception as e:
                logger.error(f"Task {task['task_number']} failed: {e}")
                self.service.update_task(task_id, status="failed")
                self.service.add_message(
                    session_id, "agent", "Implementer",
                    f"Task {task['task_number']} encountered an issue: {str(e)}",
                    message_type="error"
                )

        # Check if all tasks completed
        updated_tasks = self.service.get_tasks(session_id)
        all_complete = all(t["status"] == "completed" for t in updated_tasks)

        if all_complete:
            self.service.update_session_status(session_id, "testing")
        else:
            failed_tasks = [t for t in updated_tasks if t["status"] == "failed"]
            if failed_tasks:
                self.service.update_session_status(session_id, "failed")
                self.notification_service.notify_pipeline_failed(
                    session.get("title", "Untitled"),
                    session_id,
                    f"{len(failed_tasks)} task(s) failed"
                )

    def _run_implementer(self, system_prompt, task, user_context, task_context, workspace):
        """Run implementer agent via Claude CLI with native tools (Pro plan)."""
        try:
            task_title = task.get("title", "Unknown task") if isinstance(task, dict) else str(task)
            task_desc = task.get("description", "") if isinstance(task, dict) else ""

            prompt = (
                f"You are implementing a code change for {config.APP_DESCRIPTION}.\n\n"
                f"TASK: {task_title}\n"
                f"DESCRIPTION: {task_desc}\n\n"
                f"USER REQUEST:\n{user_context}\n\n"
                f"IMPLEMENTATION PLAN:\n{task_context}\n\n"
                f"WORKSPACE: The codebase is at {workspace}\n\n"
                "Instructions:\n"
                "1. Read the relevant files to understand the current code\n"
                "2. Make the necessary changes using the Edit tool for surgical changes or Write for new files\n"
                '3. Verify your changes compile: run python -c "import py_compile; py_compile.compile(\'path/to/file.py\', doraise=True)"\n'
                "4. At the end, output a JSON summary on a single line like:\n"
                '   {"files_changed": ["path1.py", "path2.html"], "summary": "what you did"}\n\n'
                "SAFETY RULES:\n"
                "- Do NOT modify .env files or anything containing credentials/secrets\n"
                "- Do NOT run destructive commands (rm -rf, DROP TABLE, etc.)\n"
                "- Do NOT push to git - the orchestrator handles that\n"
                "- Only modify files within the workspace directory"
            )

            cmd = [
                "claude", "-p", prompt,
                "--model", self.model,
                "--output-format", "text",
                "--max-turns", "20",
            ]
            if system_prompt:
                cmd.extend(["--system-prompt", system_prompt])

            env = {
                **os.environ,
                "CI": "true",
                "TERM": "dumb",
                "HOME": config.VM_HOME,
                "USER": config.VM_USER,
            }

            logger.info(f"Running implementer via Claude CLI in {workspace}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                stdin=subprocess.DEVNULL,
                cwd=workspace,
                env=env,
            )

            output = result.stdout.strip()

            if result.returncode != 0:
                logger.error(f"Implementer CLI error (rc={result.returncode}): {result.stderr[:500]}")
                return {"success": False, "error": result.stderr[:500], "output": output, "files_changed": []}

            # Try to extract files_changed from git diff
            files_changed = []
            try:
                git_result = subprocess.run(
                    ["git", "diff", "--name-only"],
                    capture_output=True, text=True,
                    cwd=workspace, timeout=10,
                )
                files_changed = [f.strip() for f in git_result.stdout.strip().split("\n") if f.strip()]
            except Exception:
                pass

            # Also check for untracked files
            try:
                git_result = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    capture_output=True, text=True,
                    cwd=workspace, timeout=10,
                )
                untracked = [f.strip() for f in git_result.stdout.strip().split("\n") if f.strip()]
                files_changed.extend(untracked)
            except Exception:
                pass

            logger.info(f"Implementer completed. Files changed: {files_changed}")

            return {
                "success": True,
                "output": output,
                "files_changed": files_changed,
            }

        except subprocess.TimeoutExpired:
            logger.error("Implementer timed out after 900s")
            return {"success": False, "error": "Timed out after 900s", "files_changed": []}
        except Exception as e:
            logger.error(f"Implementer failed: {e}")
            return {"success": False, "error": str(e), "files_changed": []}
    def _get_implementer_tools(self):
        """Define the sandboxed tools available to the Implementer agent."""
        return [
            {
                "name": "read_file",
                "description": "Read the contents of a file in the workspace or main codebase (read-only for codebase).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to workspace or absolute backend path"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": "Write content to a file in the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to workspace"},
                        "content": {"type": "string", "description": "File content to write"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "list_directory",
                "description": "List files in a directory.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "search_codebase",
                "description": "Search for a pattern in the codebase using grep.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Search pattern (regex)"},
                        "file_glob": {"type": "string", "description": "File glob pattern (e.g., '*.py')"},
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "run_python_check",
                "description": "Run a Python syntax check on a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to check"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "run_tests",
                "description": "Run pytest on a specific test file or directory.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Test file or directory"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "git_diff",
                "description": "Show git diff of current changes in the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "git_commit",
                "description": "Commit current changes with a message.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Commit message"},
                    },
                    "required": ["message"],
                },
            },
        ]

    def _execute_tool(self, tool_name, params, workspace):
        """Execute a sandboxed tool and return the result."""
        try:
            if tool_name == "read_file":
                return self._tool_read_file(params["path"], workspace)
            elif tool_name == "write_file":
                return self._tool_write_file(params["path"], params["content"], workspace)
            elif tool_name == "list_directory":
                return self._tool_list_directory(params["path"], workspace)
            elif tool_name == "search_codebase":
                return self._tool_search_codebase(
                    params["pattern"], params.get("file_glob", ""), workspace
                )
            elif tool_name == "run_python_check":
                return self._tool_run_python_check(params["path"], workspace)
            elif tool_name == "run_tests":
                return self._tool_run_tests(params["path"], workspace)
            elif tool_name == "git_diff":
                return self._tool_git_diff(workspace)
            elif tool_name == "git_commit":
                return self._tool_git_commit(params["message"], workspace)
            else:
                return f"Unknown tool: {tool_name}"
        except Exception as e:
            return f"Tool error: {str(e)}"

    # =========================================================================
    # SANDBOXED TOOL IMPLEMENTATIONS
    # =========================================================================

    def _validate_path(self, path, workspace, allow_codebase_read=False):
        """Validate a file path is within allowed boundaries."""
        # Check for forbidden patterns
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in path:
                raise ValueError(f"Path contains forbidden pattern: {pattern}")

        # Resolve to absolute path
        if os.path.isabs(path):
            abs_path = os.path.realpath(path)
        else:
            abs_path = os.path.realpath(os.path.join(workspace, path))

        # Must be within workspace (for writes) or codebase (for reads)
        workspace_real = os.path.realpath(workspace)
        if abs_path.startswith(workspace_real):
            return abs_path

        if allow_codebase_read:
            # Allow reading from the main backend directory
            backend_dir = config.BACKEND_DIR or config.WORKING_DIR
            if abs_path.startswith(os.path.realpath(backend_dir)):
                return abs_path

        raise ValueError(f"Path outside allowed directories: {path}")

    def _tool_read_file(self, path, workspace):
        """Read a file from workspace or codebase."""
        abs_path = self._validate_path(path, workspace, allow_codebase_read=True)
        if not os.path.exists(abs_path):
            return f"File not found: {path}"

        ext = os.path.splitext(abs_path)[1]
        if ext and ext not in ALLOWED_EXTENSIONS:
            return f"File type not allowed: {ext}"

        with open(abs_path, "r", errors="replace") as f:
            content = f.read()

        # Truncate very large files
        if len(content) > 50000:
            content = content[:50000] + "\n... (truncated)"

        return content

    def _tool_write_file(self, path, content, workspace):
        """Write a file to the workspace only."""
        abs_path = self._validate_path(path, workspace)

        ext = os.path.splitext(abs_path)[1]
        if ext and ext not in ALLOWED_EXTENSIONS:
            return f"File type not allowed: {ext}"

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w") as f:
            f.write(content)

        return f"File written: {path} ({len(content)} bytes)"

    def _tool_list_directory(self, path, workspace):
        """List files in a directory."""
        abs_path = self._validate_path(path, workspace, allow_codebase_read=True)
        if not os.path.isdir(abs_path):
            return f"Not a directory: {path}"

        entries = sorted(os.listdir(abs_path))[:100]  # Limit to 100 entries
        result = []
        for entry in entries:
            full = os.path.join(abs_path, entry)
            indicator = "/" if os.path.isdir(full) else ""
            result.append(f"{entry}{indicator}")

        return "\n".join(result) if result else "(empty directory)"

    def _tool_search_codebase(self, pattern, file_glob, workspace):
        """Search the codebase using grep."""
        search_dir = os.path.join(config.BACKEND_DIR or config.WORKING_DIR, "app")
        if not os.path.isdir(search_dir):
            search_dir = workspace

        cmd = ["grep", "-rn", "--include", file_glob or "*.py", pattern, search_dir]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            output = result.stdout[:10000]  # Limit output
            if not output:
                return f"No matches found for: {pattern}"
            return output
        except subprocess.TimeoutExpired:
            return "Search timed out"
        except Exception as e:
            return f"Search error: {e}"

    def _tool_run_python_check(self, path, workspace):
        """Run py_compile syntax check."""
        abs_path = self._validate_path(path, workspace)
        if not abs_path.endswith(".py"):
            return "Not a Python file"

        try:
            result = subprocess.run(
                ["python3", "-m", "py_compile", abs_path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return f"Syntax OK: {path}"
            return f"Syntax error: {result.stderr}"
        except Exception as e:
            return f"Check error: {e}"

    def _tool_run_tests(self, path, workspace):
        """Run pytest with timeout."""
        abs_path = self._validate_path(path, workspace)

        try:
            result = subprocess.run(
                ["python3", "-m", "pytest", abs_path, "-v", "--tb=short", "--timeout=60"],
                capture_output=True, text=True, timeout=120,
                cwd=workspace,
            )
            output = result.stdout[-5000:] + "\n" + result.stderr[-2000:]
            return output.strip()
        except subprocess.TimeoutExpired:
            return "Tests timed out (2 min limit)"
        except Exception as e:
            return f"Test error: {e}"

    def _tool_git_diff(self, workspace):
        """Show git diff in workspace."""
        try:
            result = subprocess.run(
                ["git", "diff"], capture_output=True, text=True,
                timeout=10, cwd=workspace
            )
            return result.stdout[:10000] or "(no changes)"
        except Exception as e:
            return f"Git error: {e}"

    def _tool_git_commit(self, message, workspace):
        """Git add + commit in workspace."""
        try:
            subprocess.run(
                ["git", "add", "."], capture_output=True, text=True,
                timeout=10, cwd=workspace
            )
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True, text=True, timeout=10, cwd=workspace
            )
            return result.stdout or result.stderr
        except Exception as e:
            return f"Git error: {e}"

    # =========================================================================
    # PHASE 4: TESTING
    # =========================================================================

    def _handle_testing(self, session):
        """Run automated tests on the workspace code."""
        session_id = session["id"]
        workspace = os.path.join(WORKSPACE_BASE, session_id)

        self.service.add_message(
            session_id, "system", "System",
            "Running automated tests...",
            message_type="status_update"
        )

        # Run pytest if there are test files
        test_dir = os.path.join(workspace, "tests")
        if os.path.isdir(test_dir):
            result = self._tool_run_tests(test_dir, workspace)
            self.service.add_message(
                session_id, "agent", "Test Runner",
                f"Test results:\n```\n{result}\n```",
                message_type="status_update"
            )

        # Move to deploying
        self.service.update_session_status(session_id, "deploying_staging")

    # =========================================================================
    # PHASE 5: DEPLOYING TO STAGING
    # =========================================================================

    def _handle_deploying(self, session):
        """Deploy changes to staging environment."""
        session_id = session["id"]
        workspace = os.path.join(WORKSPACE_BASE, session_id)

        self.service.add_message(
            session_id, "system", "System",
            "Deploying to staging...",
            message_type="status_update"
        )

        try:
            # Get the latest commit SHA
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, cwd=workspace
            )
            commit_sha = result.stdout.strip()[:40] if result.returncode == 0 else ""

            # Run deploy-staging.sh if it exists
            deploy_script = config.STAGING_DEPLOY_SCRIPT
            if os.path.exists(deploy_script):
                deploy_result = subprocess.run(
                    ["bash", deploy_script],
                    capture_output=True, text=True, timeout=300, cwd=workspace
                )
                if deploy_result.returncode != 0:
                    raise RuntimeError(f"Deploy failed: {deploy_result.stderr}")

            staging_url = config.STAGING_URL

            self.service.update_session(
                session_id,
                status="completed",
                staging_url=staging_url,
                deploy_commit_sha=commit_sha,
                summary="Changes deployed to staging successfully."
            )

            self.service.add_message(
                session_id, "system", "System",
                f"Deployed to staging! Test at: {staging_url}\n\n"
                f"Commit: {commit_sha}\n\n"
                f"To push to production, contact Ian.",
                message_type="status_update"
            )

            self.notification_service.notify_deployed_staging(
                session.get("title", "Untitled"),
                session_id,
                staging_url,
                commit_sha
            )

        except Exception as e:
            self.service.update_session_status(session_id, "failed")
            self.notification_service.notify_pipeline_failed(
                session.get("title", "Untitled"),
                session_id,
                str(e)
            )
            raise

    # =========================================================================
    # CLAUDE API HELPERS
    # =========================================================================

    def _call_claude(self, system_prompt, messages, timeout=120):
        """Call Claude via CLI using Pro plan OAuth tokens."""
        try:
            # Build the prompt from the messages list
            prompt_parts = []
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            prompt_parts.append(block["text"])
            prompt = "\n\n".join(prompt_parts)

            cmd = ["claude", "-p", prompt, "--model", self.model, "--output-format", "text"]
            if system_prompt:
                cmd.extend(["--system-prompt", system_prompt])

            env = {
                **os.environ,
                "CI": "true",
                "TERM": "dumb",
                "HOME": config.VM_HOME,
                "USER": config.VM_USER,
            }

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env=env,
            )

            if result.returncode != 0:
                logger.error(f"Claude CLI error (rc={result.returncode}): {result.stderr[:500]}")
                return None

            output = result.stdout.strip()
            if not output:
                logger.error("Claude CLI returned empty output")
                return None

            return output

        except subprocess.TimeoutExpired:
            logger.error(f"Claude CLI timed out after {timeout}s")
            return None
        except Exception as e:
            logger.error(f"Claude CLI call failed: {e}")
            return None
    def _build_conversation(self, messages):
        """Convert AI Ops messages into Claude API conversation format."""
        conversation = []
        for msg in messages:
            if msg["sender_type"] == "user":
                conversation.append({
                    "role": "user",
                    "content": msg["content"],
                })
            elif msg["sender_type"] in ("agent", "system"):
                conversation.append({
                    "role": "assistant",
                    "content": msg["content"],
                })

        # Ensure conversation starts with user and alternates
        cleaned = []
        last_role = None
        for msg in conversation:
            if msg["role"] == last_role:
                # Merge consecutive same-role messages
                cleaned[-1]["content"] += "\n\n" + msg["content"]
            else:
                cleaned.append(msg)
                last_role = msg["role"]

        # Ensure it starts with user
        if cleaned and cleaned[0]["role"] != "user":
            cleaned.insert(0, {"role": "user", "content": "(session started)"})

        return cleaned

    def _parse_json_response(self, text):
        """Parse JSON from Claude response, handling markdown code blocks."""
        text = text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                return json.loads(match.group())
            raise

    # =========================================================================
    # MANUAL TRIGGER (for route handlers)
    # =========================================================================

    def process_user_message(self, session_id, message_content, user_id, user_name):
        """Called by the route when a user sends a message."""
        session = self.service.get_session(session_id)
        if not session:
            return {"error": "Session not found"}

        # Store the user message
        self.service.add_message(
            session_id, "user", user_name, message_content,
            message_type="chat"
        )

        # If session is awaiting_approval and user says something,
        # check if it's approval or feedback
        if session["status"] == "awaiting_approval":
            lower = message_content.lower().strip()
            if any(w in lower for w in ["approve", "looks good", "go ahead", "yes", "lgtm"]):
                self.approve_plan(session_id, user_id)
                return {"status": "approved"}
            else:
                # User has questions — add as feedback
                self.service.add_message(
                    session_id, "system", "System",
                    "Your feedback has been noted. The planning team will revise the plan.",
                    message_type="status_update"
                )
                self.service.update_session_status(session_id, "planning")
                return {"status": "revising"}

        return {"status": "message_received"}

    def approve_plan(self, session_id, user_id=None):
        """Approve the task plan and start coding."""
        self.service.update_session_status(session_id, "coding", user_id)
        self.service.log_audit(session_id, user_id, "plan_approved")
        self.service.add_message(
            session_id, "system", "System",
            "Plan approved! The coding agents are starting work...",
            message_type="status_update"
        )
