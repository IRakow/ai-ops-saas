"""
AI OPS - UNIFIED AGENT SYSTEM
==============================
Handles BOTH error triage and feature planning through the same
3-agent consensus pipeline.

ERROR MODE:  Diagnostician + Engineer + Reviewer
FEATURE MODE: Architect + Engineer + QA Agent

Both use Q&A interrogation — agents must ask and answer questions
until they're satisfied before voting.

Usage:
    # Error triage (automatic, runs in background)
    agent.start()  # Polls error log every 5 minutes

    # Feature planning (on demand)
    result = agent.plan_feature(
        name="Tenant SMS Notifications",
        description="Send SMS to tenants when work orders are updated",
        requirements=["Twilio integration", "Opt-in/out per tenant"],
        related_files=["routes/work_orders.py", "models/tenant.py"],
    )

    # Or via API
    POST /agent/feature
    {
        "name": "Tenant SMS Notifications",
        "description": "Send SMS when work orders updated",
        "requirements": ["Twilio integration"],
        "related_files": ["routes/work_orders.py"]
    }
"""

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict

from .consensus_engine import ConsensusEngine, ConsensusResult, MAX_QA_ROUNDS
from .notifications import NotificationManager, NotificationConfig

logger = logging.getLogger("ai_ops.agent")


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class ErrorEvent:
    timestamp: str
    service: str
    error_type: str
    error_message: str
    traceback: str
    endpoint: str = ""
    circuit_state: str = ""

    @property
    def fingerprint(self):
        import hashlib
        key = f"{self.error_type}:{self.error_message}:{self.endpoint}"
        return hashlib.md5(key.encode()).hexdigest()[:12]


@dataclass
class TaskRecord:
    """Tracks any task — error fix or feature plan."""
    fingerprint: str
    mode: str  # "error" or "feature"
    title: str
    description: str
    status: str  # new, analyzing, consensus_reached, no_consensus,
                 # approved, applied, ignored
    count: int = 1
    first_seen: str = ""
    last_seen: str = ""
    consensus: ConsensusResult = None
    # Error-specific
    sample_traceback: str = ""
    service: str = ""
    endpoint: str = ""
    # Feature-specific
    requirements: list = field(default_factory=list)
    related_files: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    existing_code_context: str = ""


# =============================================================================
# UNIFIED AGENT
# =============================================================================

class AIOpsAgent:
    """
    Unified agent for AI Ops Debugger.
    Handles error triage (background) and feature planning (on-demand).
    """

    def __init__(
        self,
        anthropic_api_key: str = None,
        resilience_manager=None,
        project_root: str = ".",
        notification_config: NotificationConfig = None,
        poll_interval: int = 300,
        auto_fix_enabled: bool = False,
        auto_fix_categories: list = None,
        output_dir: str = None,
        error_log_path: str = None,
    ):
        self.api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.resilience_manager = resilience_manager
        self.project_root = Path(project_root)
        self.poll_interval = poll_interval
        self.auto_fix_enabled = auto_fix_enabled
        self.auto_fix_categories = set(auto_fix_categories or [])
        self.output_dir = Path(output_dir or self.project_root / "agent_output")
        self.error_log_path = error_log_path

        self.consensus_engine = ConsensusEngine(api_key=self.api_key)
        self.notifier = NotificationManager(
            config=notification_config or NotificationConfig()
        )

        self.tasks = {}  # fingerprint -> TaskRecord
        self.processed_error_fps = set()
        self._running = False
        self._thread = None
        self._last_poll = None

        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "errors").mkdir(exist_ok=True)
        (self.output_dir / "features").mkdir(exist_ok=True)

    # -------------------------------------------------------------------------
    # LIFECYCLE (error triage runs in background)
    # -------------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"🤖 AI Ops Agent started (error triage every {self.poll_interval}s, "
            f"feature planning on-demand, Claude Opus 4.6)"
        )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _poll_loop(self):
        while self._running:
            try:
                self._run_error_triage()
            except Exception as e:
                logger.error(f"Error triage cycle failed: {e}", exc_info=True)
            time.sleep(self.poll_interval)

    # -------------------------------------------------------------------------
    # ERROR TRIAGE (automatic)
    # -------------------------------------------------------------------------

    def _run_error_triage(self):
        errors = self._collect_errors()
        if not errors:
            return

        new_groups = self._group_errors(errors)
        if not new_groups:
            return

        logger.info(f"Triaging {len(new_groups)} new error(s)")

        for task in new_groups:
            task.status = "analyzing"

            source_context = self._extract_source_context(task.sample_traceback)

            consensus = self.consensus_engine.analyze_error(
                error_type=task.title,
                error_message=task.description,
                traceback_text=task.sample_traceback,
                source_context=source_context,
                service=task.service,
                endpoint=task.endpoint,
                fingerprint=task.fingerprint,
            )

            task.consensus = consensus
            task.status = "consensus_reached" if consensus.consensus_reached else "no_consensus"

            self._save_task(task)
            self.notifier.send(consensus, severity=consensus.severity)

            if (self.auto_fix_enabled and consensus.auto_fixable
                    and task.title in self.auto_fix_categories):
                self._apply_fix(task)

        self._last_poll = datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # FEATURE PLANNING (on-demand)
    # -------------------------------------------------------------------------

    def plan_feature(self, name, description, requirements=None,
                     related_files=None, constraints=None,
                     existing_code_context=None) -> ConsensusResult:
        """
        Plan a feature using 3-agent consensus.
        Returns the full consensus result with architecture,
        implementation plan, testing strategy, and rollback plan.
        """
        # Auto-read related files if paths provided
        code_context = existing_code_context or ""
        if related_files and not code_context:
            code_context = self._read_related_files(related_files)

        consensus = self.consensus_engine.plan_feature(
            feature_name=name,
            feature_description=description,
            existing_code_context=code_context,
            related_files=related_files or [],
            requirements=requirements or [],
            constraints=constraints or [],
        )

        # Save as a task record
        task = TaskRecord(
            fingerprint=consensus.fingerprint,
            mode="feature",
            title=name,
            description=description,
            status="consensus_reached" if consensus.consensus_reached else "no_consensus",
            first_seen=datetime.now(timezone.utc).isoformat(),
            consensus=consensus,
            requirements=requirements or [],
            related_files=related_files or [],
            constraints=constraints or [],
            existing_code_context=code_context,
        )
        self.tasks[task.fingerprint] = task
        self._save_task(task)

        # Notify
        self.notifier.send(consensus, severity=consensus.risk_level or "medium")

        return consensus

    def _read_related_files(self, file_paths):
        """Read source code from related files to give agents context."""
        parts = []
        for fp in file_paths:
            full_path = self.project_root / fp
            try:
                if full_path.exists():
                    with open(full_path, "r") as f:
                        content = f.read()
                    # Truncate long files
                    if len(content) > 3000:
                        content = content[:3000] + "\n... (truncated)"
                    parts.append(f"\n--- {fp} ---\n{content}")
            except Exception as e:
                parts.append(f"\n--- {fp} ---\n(Error reading: {e})")
        return "\n".join(parts) if parts else "(no files read)"

    # -------------------------------------------------------------------------
    # ERROR COLLECTION & GROUPING
    # -------------------------------------------------------------------------

    def _collect_errors(self):
        errors = []

        if self.resilience_manager:
            for e in self.resilience_manager.get_recent_errors(limit=100):
                errors.append(ErrorEvent(
                    timestamp=e.get("timestamp", ""),
                    service=e.get("service", "unknown"),
                    error_type=e.get("error_type", "Unknown"),
                    error_message=e.get("error_message", ""),
                    traceback=e.get("traceback", ""),
                    circuit_state=e.get("circuit_state", ""),
                ))

        if self.error_log_path and os.path.exists(self.error_log_path):
            errors.extend(self._parse_error_log(self.error_log_path))

        flask_log = self.project_root / "logs" / "error.log"
        if flask_log.exists():
            errors.extend(self._parse_error_log(str(flask_log)))

        return errors

    def _group_errors(self, errors):
        new_tasks = []

        for error in errors:
            fp = error.fingerprint

            if fp in self.tasks:
                self.tasks[fp].count += 1
                self.tasks[fp].last_seen = error.timestamp
            else:
                task = TaskRecord(
                    fingerprint=fp,
                    mode="error",
                    title=error.error_type,
                    description=error.error_message,
                    status="new",
                    first_seen=error.timestamp,
                    last_seen=error.timestamp,
                    sample_traceback=error.traceback,
                    service=error.service,
                    endpoint=error.endpoint,
                )
                self.tasks[fp] = task

                if fp not in self.processed_error_fps:
                    new_tasks.append(task)
                    self.processed_error_fps.add(fp)

        return new_tasks

    def _parse_error_log(self, log_path):
        errors = []
        try:
            with open(log_path, "r") as f:
                content = f.read()

            blocks = re.split(r'Traceback \(most recent call last\):', content)
            for block in blocks[1:]:
                lines = block.strip().split("\n")
                if not lines:
                    continue
                error_line = lines[-1]
                parts = error_line.split(":", 1)
                errors.append(ErrorEvent(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    service="flask",
                    error_type=parts[0].strip() if parts else "Unknown",
                    error_message=parts[1].strip() if len(parts) > 1 else "",
                    traceback="Traceback (most recent call last):" + block,
                ))
        except Exception as e:
            logger.error(f"Log parse failed: {e}")
        return errors

    def _extract_source_context(self, tb_text, context_lines=10):
        parts = []
        for match in re.finditer(r'File "([^"]+)", line (\d+)', tb_text):
            filepath, line_num = match.group(1), int(match.group(2))
            if "site-packages" in filepath or "/usr/lib" in filepath:
                continue
            try:
                source_path = Path(filepath)
                if not source_path.exists():
                    source_path = self.project_root / filepath
                if source_path.exists():
                    with open(source_path, "r") as f:
                        lines = f.readlines()
                    start = max(0, line_num - context_lines - 1)
                    end = min(len(lines), line_num + context_lines)
                    snippet = "".join(
                        f"{'>>> ' if i == line_num - 1 else '    '}{i + 1}: {line}"
                        for i, line in enumerate(lines[start:end], start=start)
                    )
                    parts.append(f"\n--- {filepath} (line {line_num}) ---\n{snippet}")
            except Exception:
                continue
        return "\n".join(parts) if parts else "(source not available)"

    # -------------------------------------------------------------------------
    # APPLY FIX / SAVE
    # -------------------------------------------------------------------------

    def _apply_fix(self, task):
        if not task.consensus or not task.consensus.final_fix_diff:
            return False
        try:
            target = self.project_root / task.consensus.final_fix_file
            if not target.exists():
                return False
            import shutil
            backup = target.with_suffix(f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(target, backup)

            import subprocess
            result = subprocess.run(
                ["patch", "-p1", str(target)],
                input=task.consensus.final_fix_diff,
                capture_output=True, text=True,
                cwd=str(self.project_root),
            )
            if result.returncode == 0:
                task.status = "applied"
                logger.info(f"✅ Auto-fix applied: {task.consensus.final_fix_file}")
                return True
        except Exception as e:
            logger.error(f"Fix apply failed: {e}")
        return False

    def _save_task(self, task):
        subdir = "errors" if task.mode == "error" else "features"
        path = self.output_dir / subdir / f"{task.fingerprint}.json"

        data = {
            "fingerprint": task.fingerprint,
            "mode": task.mode,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "count": task.count,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        if task.consensus:
            data["consensus"] = asdict(task.consensus)

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

        # Save readable transcript
        if task.consensus and task.consensus.debate_transcript:
            transcript_path = self.output_dir / subdir / f"{task.fingerprint}_debate.txt"
            with open(transcript_path, "w") as f:
                f.write(task.consensus.debate_transcript)

    # -------------------------------------------------------------------------
    # QUERY METHODS
    # -------------------------------------------------------------------------

    def get_pending(self, mode=None):
        """Get all tasks awaiting review."""
        tasks = self.tasks.values()
        if mode:
            tasks = [t for t in tasks if t.mode == mode]

        return [
            {
                "fingerprint": t.fingerprint,
                "mode": t.mode,
                "title": t.title,
                "description": t.description[:200],
                "status": t.status,
                "count": t.count,
                "consensus_reached": t.consensus.consensus_reached if t.consensus else False,
                "votes": t.consensus.vote_summary if t.consensus else {},
                "confidence": t.consensus.confidence if t.consensus else 0,
                "qa_rounds": t.consensus.qa_rounds_completed if t.consensus else 0,
                "all_questions_resolved": t.consensus.all_questions_resolved if t.consensus else False,
                "unresolved_concerns": t.consensus.unresolved_concerns if t.consensus else [],
                # Error fields
                "severity": t.consensus.severity if t.consensus else "",
                "auto_fixable": t.consensus.auto_fixable if t.consensus else False,
                "fix_file": t.consensus.final_fix_file if t.consensus else "",
                # Feature fields
                "complexity": t.consensus.complexity if t.consensus else "",
                "effort_estimate": t.consensus.effort_estimate if t.consensus else "",
                "risk_level": t.consensus.risk_level if t.consensus else "",
            }
            for t in tasks
            if t.status in ("consensus_reached", "no_consensus")
        ]

    def get_debate(self, fingerprint):
        task = self.tasks.get(fingerprint)
        if not task or not task.consensus:
            return {"error": "Not found"}
        return {
            "fingerprint": fingerprint,
            "mode": task.mode,
            "title": task.title,
            "transcript": task.consensus.debate_transcript,
            "votes": task.consensus.vote_summary,
            "consensus": task.consensus.consensus_reached,
            "qa_rounds": task.consensus.qa_rounds_completed,
            "all_resolved": task.consensus.all_questions_resolved,
            "unresolved": task.consensus.unresolved_concerns,
        }

    def get_feature_plan(self, fingerprint):
        """Get the full feature plan for a completed feature analysis."""
        task = self.tasks.get(fingerprint)
        if not task or task.mode != "feature" or not task.consensus:
            return {"error": "Not found or not a feature"}

        c = task.consensus
        return {
            "fingerprint": fingerprint,
            "feature_name": c.feature_name,
            "consensus_reached": c.consensus_reached,
            "votes": c.vote_summary,
            "confidence": c.confidence,
            "architecture": c.architecture,
            "implementation_plan": c.implementation_plan,
            "implementation_steps": c.implementation_steps,
            "files_to_change": c.files_to_change,
            "new_files": c.new_files,
            "effort_estimate": c.effort_estimate,
            "complexity": c.complexity,
            "risk_level": c.risk_level,
            "testing_strategy": c.testing_strategy,
            "rollback_plan": c.rollback_plan,
            "edge_cases": c.edge_cases,
            "unresolved_concerns": c.unresolved_concerns,
        }

    def approve(self, fingerprint):
        task = self.tasks.get(fingerprint)
        if not task:
            return {"error": "Not found"}
        if task.mode == "error" and task.consensus and task.consensus.consensus_reached:
            success = self._apply_fix(task)
            return {"applied": success}
        elif task.mode == "feature":
            task.status = "approved"
            self._save_task(task)
            return {"status": "approved", "plan": self.get_feature_plan(fingerprint)}
        return {"error": "Cannot approve — no consensus"}

    def ignore(self, fingerprint):
        if fingerprint in self.tasks:
            self.tasks[fingerprint].status = "ignored"
            return {"status": "ignored"}
        return {"error": "Not found"}

    def get_status(self):
        error_count = sum(1 for t in self.tasks.values() if t.mode == "error")
        feature_count = sum(1 for t in self.tasks.values() if t.mode == "feature")

        return {
            "running": self._running,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "model": "claude-opus-4-6",
            "error_agents": ["Diagnostician", "Engineer", "Reviewer"],
            "feature_agents": ["Architect", "Engineer", "QA Agent"],
            "consensus_method": "Q&A interrogation + 2/3 majority vote",
            "max_qa_rounds": MAX_QA_ROUNDS,
            "total_errors": error_count,
            "total_features": feature_count,
            "pending": len(self.get_pending()),
            "poll_interval": self.poll_interval,
            "auto_fix_enabled": self.auto_fix_enabled,
        }


# =============================================================================
# FLASK INTEGRATION
# =============================================================================

def init_flask_agent(app, agent: AIOpsAgent):
    """Register all agent endpoints."""

    # ── Status & listing ──────────────────────────────────────────────

    @app.route("/agent/status")
    def agent_status():
        from flask import jsonify
        return jsonify(agent.get_status())

    @app.route("/agent/pending")
    def agent_pending():
        from flask import jsonify, request
        mode = request.args.get("mode")  # ?mode=error or ?mode=feature
        return jsonify(agent.get_pending(mode))

    # ── Error triage ──────────────────────────────────────────────────

    @app.route("/agent/triage", methods=["POST"])
    def agent_triage_now():
        from flask import jsonify
        agent._run_error_triage()
        return jsonify({"status": "completed"})

    # ── Feature planning ──────────────────────────────────────────────

    @app.route("/agent/feature", methods=["POST"])
    def agent_plan_feature():
        from flask import jsonify, request
        data = request.json

        name = data.get("name", "")
        description = data.get("description", "")

        if not name or not description:
            return jsonify({"error": "name and description required"}), 400

        result = agent.plan_feature(
            name=name,
            description=description,
            requirements=data.get("requirements"),
            related_files=data.get("related_files"),
            constraints=data.get("constraints"),
            existing_code_context=data.get("code_context"),
        )

        return jsonify({
            "fingerprint": result.fingerprint,
            "consensus_reached": result.consensus_reached,
            "votes": result.vote_summary,
            "confidence": result.confidence,
            "qa_rounds": result.qa_rounds_completed,
            "plan": agent.get_feature_plan(result.fingerprint),
        })

    @app.route("/agent/feature/<fingerprint>")
    def agent_feature_plan(fingerprint):
        from flask import jsonify
        return jsonify(agent.get_feature_plan(fingerprint))

    # ── Shared actions ────────────────────────────────────────────────

    @app.route("/agent/debate/<fingerprint>")
    def agent_debate(fingerprint):
        from flask import jsonify
        return jsonify(agent.get_debate(fingerprint))

    @app.route("/agent/approve/<fingerprint>", methods=["POST"])
    def agent_approve(fingerprint):
        from flask import jsonify
        return jsonify(agent.approve(fingerprint))

    @app.route("/agent/ignore/<fingerprint>", methods=["POST"])
    def agent_ignore(fingerprint):
        from flask import jsonify
        return jsonify(agent.ignore(fingerprint))

    @app.route("/agent/output")
    def agent_output():
        """List all saved output files."""
        from flask import jsonify
        files = []
        for subdir in ["errors", "features"]:
            dir_path = agent.output_dir / subdir
            for f in dir_path.glob("*.json"):
                with open(f) as fh:
                    files.append(json.load(fh))
        return jsonify(files)
