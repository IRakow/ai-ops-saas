"""
AGENT CONSENSUS ENGINE v3
=========================
3 Claude Opus 4.6 agents that work in TWO MODES:

MODE 1: ERROR TRIAGE
    🔍 Diagnostician → Root cause analysis
    🔧 Engineer      → Code fix
    🛡️ Reviewer      → Safety & edge cases

MODE 2: FEATURE PLANNING
    🏗️ Architect     → System design, data model, integration points
    🔧 Engineer      → Implementation plan, code structure, effort estimate
    🛡️ QA Agent      → Edge cases, testing strategy, rollback plan

Both modes use the same Q&A interrogation flow:
    Phase 1: Independent analysis
    Phase 2: Q&A rounds (agents ask each other questions, up to 4 rounds)
    Phase 3: Final vote (2/3 majority required)

The agents must reach certainty through questioning before voting.
"""

import os
import re
import json
import logging
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

import config

logger = logging.getLogger("ai_ops.consensus")

MODEL = "claude-opus-4-6"
MAX_QA_ROUNDS = 4

CODEBASE_CONTEXT = config.get_codebase_context()


# =============================================================================
# AGENT PROFILES — ERROR TRIAGE MODE
# =============================================================================

ERROR_AGENTS = {
    "diagnostician": {
        "name": "Diagnostician",
        "emoji": "🔍",
        "system_prompt": f"""You are the DIAGNOSTICIAN on a 3-agent debugging team.
{CODEBASE_CONTEXT}

YOUR ROLE: Root cause analysis. Figure out WHY the error happened.

DURING Q&A: Interrogate the Engineer. Don't accept surface-level fixes:
- "Your fix handles the null case, but what CAUSED the null?"
- "This is a race condition. Does your fix handle concurrent requests?"
- "You're catching the exception — won't the retry cause duplicate data?"

RESPONSE FORMAT (JSON):
{{
    "analysis": "your understanding of the problem",
    "questions_for": {{"engineer": ["questions"], "reviewer": ["questions"]}},
    "answers": {{"question asked of you": "your answer"}},
    "remaining_concerns": ["unresolved issues"],
    "satisfied": true/false,
    "severity": "critical|high|medium|low",
    "confidence": 0.0-1.0
}}

Set "satisfied" to true ONLY when the root cause is properly identified and addressed.""",
    },
    "engineer": {
        "name": "Engineer",
        "emoji": "🔧",
        "system_prompt": f"""You are the ENGINEER on a 3-agent debugging team.
{CODEBASE_CONTEXT}

YOUR ROLE: Write the code fix. Defend it under questioning.

The others WILL grill you. Answer honestly. If a question reveals a flaw, UPDATE YOUR FIX.

RESPONSE FORMAT (JSON):
{{
    "analysis": "your fix proposal and reasoning",
    "proposed_fix": "description of what you're fixing",
    "fix_file": "relative/path/to/file.py",
    "fix_diff": "unified diff",
    "questions_for": {{"diagnostician": ["questions"], "reviewer": ["questions"]}},
    "answers": {{"question asked of you": "your answer, with updated fix if needed"}},
    "remaining_concerns": ["things you need clarified"],
    "satisfied": true/false,
    "confidence": 0.0-1.0
}}""",
    },
    "reviewer": {
        "name": "Reviewer",
        "emoji": "🛡️",
        "system_prompt": f"""You are the REVIEWER on a 3-agent debugging team.
{CODEBASE_CONTEXT}

YOUR ROLE: Safety and quality. Assume the fix might break something.

INTERROGATE the Engineer about safety:
- "What happens to in-flight requests when this deploys?"
- "Could this cause data loss if the database drops mid-write?"
- "If this fix fails, what's the blast radius?"

ALWAYS flag for manual review if the fix touches: payments, auth, critical documents, schema changes, data deletion.

RESPONSE FORMAT (JSON):
{{
    "analysis": "your safety assessment",
    "questions_for": {{"engineer": ["safety questions"], "diagnostician": ["questions"]}},
    "answers": {{"question asked of you": "your answer"}},
    "remaining_concerns": ["unresolved safety issues"],
    "satisfied": true/false,
    "safe_to_auto_fix": true/false,
    "confidence": 0.0-1.0
}}

Set "safe_to_auto_fix" to true ONLY for trivial fixes that can't affect payments, auth, or data.""",
    },
}


# =============================================================================
# AGENT PROFILES — FEATURE PLANNING MODE
# =============================================================================

FEATURE_AGENTS = {
    "architect": {
        "name": "Architect",
        "emoji": "🏗️",
        "system_prompt": f"""You are the ARCHITECT on a 3-agent feature planning team.
{CODEBASE_CONTEXT}

YOUR ROLE: System design. You decide HOW a feature should be built.

Focus on:
- Data model: What tables/columns in the database? Relations? Access policies?
- API design: What Flask routes? Request/response shape?
- Integration: How does this fit with existing code? What does it touch?
- Migration: Does existing data need to change? How to handle the transition?

DURING Q&A: Challenge the Engineer on implementation details:
- "How does this interact with the existing tenant query?"
- "You're adding a column — what about the existing records?"
- "This needs row-level access control. What's your isolation policy?"

Ask the QA Agent about edge cases you're worried about.

RESPONSE FORMAT (JSON):
{{
    "analysis": "your system design proposal",
    "data_model": "tables, columns, relations needed",
    "api_design": "routes, endpoints, request/response",
    "integration_points": ["existing code this touches"],
    "migration_notes": "how to handle existing data",
    "questions_for": {{"engineer": ["design questions"], "qa": ["edge case questions"]}},
    "answers": {{"question asked of you": "your answer"}},
    "remaining_concerns": ["design issues to resolve"],
    "satisfied": true/false,
    "estimated_complexity": "small|medium|large|xl",
    "confidence": 0.0-1.0
}}""",
    },
    "engineer": {
        "name": "Engineer",
        "emoji": "🔧",
        "system_prompt": f"""You are the ENGINEER on a 3-agent feature planning team.
{CODEBASE_CONTEXT}

YOUR ROLE: Implementation planning. You figure out the concrete code changes.

Focus on:
- What files need to change? What new files?
- Code structure: functions, classes, modules
- Dependencies: any new packages needed?
- Effort estimate: how long will this take?
- Implementation order: what gets built first?

DURING Q&A: The Architect will challenge your design, the QA Agent will ask about testability. Answer thoroughly. If a question reveals a better approach, update your plan.

You can ask the Architect: "Should this be a separate service or part of the main app?"
You can ask QA: "What test fixtures do we need for this?"

RESPONSE FORMAT (JSON):
{{
    "analysis": "your implementation plan",
    "files_to_change": ["list of files that need modification"],
    "new_files": ["new files to create"],
    "implementation_steps": ["ordered list of steps"],
    "code_sketch": "pseudocode or key function signatures",
    "dependencies": ["new packages if any"],
    "effort_estimate": "hours or days estimate",
    "questions_for": {{"architect": ["questions"], "qa": ["questions"]}},
    "answers": {{"question asked of you": "your answer"}},
    "remaining_concerns": ["blockers or unknowns"],
    "satisfied": true/false,
    "confidence": 0.0-1.0
}}""",
    },
    "qa": {
        "name": "QA Agent",
        "emoji": "🧪",
        "system_prompt": f"""You are the QA AGENT on a 3-agent feature planning team.
{CODEBASE_CONTEXT}

YOUR ROLE: Quality assurance and risk assessment. You think about what could go wrong.

Focus on:
- Edge cases: What about empty data? Concurrent users? Partial failures?
- Testing strategy: What tests are needed? Unit, integration, E2E?
- Rollback plan: If this breaks production, how do we undo it?
- User impact: How does this affect existing tenants and workflows?
- Data integrity: Could this corrupt existing data?

DURING Q&A: Interrogate both agents aggressively:
- "What happens if a user has no active record when this feature runs?"
- "How do you test this without affecting production data?"
- "If we need to rollback, can we do it without data loss?"
- "What's the failure mode if the database is down during migration?"

ALWAYS flag risks related to: payments, user PII, data integrity, data migration.

RESPONSE FORMAT (JSON):
{{
    "analysis": "your quality/risk assessment",
    "edge_cases": ["scenarios to handle"],
    "testing_strategy": "what tests are needed",
    "rollback_plan": "how to undo if something goes wrong",
    "user_impact": "how this affects existing users",
    "risk_level": "low|medium|high|critical",
    "questions_for": {{"architect": ["risk questions"], "engineer": ["testability questions"]}},
    "answers": {{"question asked of you": "your answer"}},
    "remaining_concerns": ["unresolved risks"],
    "satisfied": true/false,
    "safe_to_auto_implement": true/false,
    "confidence": 0.0-1.0
}}""",
    },
}


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class ConversationMessage:
    agent: str
    phase: str
    content: dict
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class ConsensusResult:
    """Result from either error triage or feature planning."""
    mode: str  # "error" or "feature"
    fingerprint: str
    consensus_reached: bool
    vote_summary: dict
    confidence: float
    qa_rounds_completed: int
    all_questions_resolved: bool
    unresolved_concerns: list
    conversation_history: list
    debate_transcript: str
    timestamp: str = ""

    # Error-specific fields
    error_type: str = ""
    error_message: str = ""
    final_diagnosis: str = ""
    final_fix: str = ""
    final_fix_file: str = ""
    final_fix_diff: str = ""
    severity: str = ""
    auto_fixable: bool = False

    # Feature-specific fields
    feature_name: str = ""
    feature_description: str = ""
    architecture: str = ""
    implementation_plan: str = ""
    implementation_steps: list = field(default_factory=list)
    files_to_change: list = field(default_factory=list)
    new_files: list = field(default_factory=list)
    effort_estimate: str = ""
    complexity: str = ""
    risk_level: str = ""
    testing_strategy: str = ""
    rollback_plan: str = ""
    edge_cases: list = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# =============================================================================
# CONSENSUS ENGINE
# =============================================================================

class ConsensusEngine:
    """
    Runs 3 agents through Q&A interrogation for either:
    - Error triage (diagnose + fix)
    - Feature planning (design + implement)
    """

    def __init__(self, api_key: str = None, model: str = MODEL,
                 max_qa_rounds: int = MAX_QA_ROUNDS):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self.max_qa_rounds = max_qa_rounds
        self.conversation: List[ConversationMessage] = []
        self.transcript_lines: List[str] = []

    # -------------------------------------------------------------------------
    # PUBLIC API
    # -------------------------------------------------------------------------

    def analyze_error(self, error_type, error_message, traceback_text,
                      source_context, service="", endpoint="",
                      fingerprint="") -> ConsensusResult:
        """Run error triage with 3-agent consensus."""

        context = {
            "mode": "error",
            "error_type": error_type,
            "error_message": error_message,
            "traceback": traceback_text,
            "source_context": source_context,
            "service": service,
            "endpoint": endpoint,
        }

        result = self._run_consensus(
            mode="error",
            agents=ERROR_AGENTS,
            context=context,
            fingerprint=fingerprint,
        )

        return result

    def plan_feature(self, feature_name, feature_description,
                     existing_code_context="", related_files=None,
                     requirements=None, constraints=None,
                     fingerprint="") -> ConsensusResult:
        """Run feature planning with 3-agent consensus."""

        context = {
            "mode": "feature",
            "feature_name": feature_name,
            "feature_description": feature_description,
            "existing_code_context": existing_code_context or "",
            "related_files": related_files or [],
            "requirements": requirements or [],
            "constraints": constraints or [],
        }

        if not fingerprint:
            import hashlib
            fingerprint = hashlib.md5(
                f"{feature_name}:{feature_description}".encode()
            ).hexdigest()[:12]

        result = self._run_consensus(
            mode="feature",
            agents=FEATURE_AGENTS,
            context=context,
            fingerprint=fingerprint,
        )

        return result

    # -------------------------------------------------------------------------
    # CORE CONSENSUS LOOP
    # -------------------------------------------------------------------------

    def _run_consensus(self, mode, agents, context, fingerprint) -> ConsensusResult:
        """Shared consensus loop for both modes."""

        self.conversation = []
        self.transcript_lines = []

        # =================================================================
        # PHASE 1: INDEPENDENT ANALYSIS
        # =================================================================
        self._log("=" * 70)
        self._log(f"PHASE 1: INDEPENDENT ANALYSIS ({mode.upper()} MODE)")
        self._log("=" * 70)

        for agent_id in agents:
            response = self._call_agent(
                agent_id=agent_id,
                agents=agents,
                phase="analysis",
                context=context,
                conversation_so_far=[],
            )
            self.conversation.append(ConversationMessage(
                agent=agent_id, phase="analysis", content=response,
            ))
            self._log_agent_response(agent_id, agents, "analysis", response)

        # =================================================================
        # PHASE 2: Q&A INTERROGATION
        # =================================================================
        self._log("\n" + "=" * 70)
        self._log("PHASE 2: Q&A INTERROGATION")
        self._log("=" * 70)

        qa_round = 0
        all_satisfied = False

        while qa_round < self.max_qa_rounds and not all_satisfied:
            qa_round += 1
            phase_name = f"qa_round_{qa_round}"

            self._log(f"\n--- Q&A Round {qa_round}/{self.max_qa_rounds} ---")

            pending_questions = self._collect_pending_questions(agents)

            if not pending_questions and qa_round > 1:
                self._log("No pending questions — all agents satisfied")
                all_satisfied = True
                break

            round_responses = {}
            for agent_id in agents:
                questions_for_me = pending_questions.get(agent_id, [])

                response = self._call_agent(
                    agent_id=agent_id,
                    agents=agents,
                    phase=phase_name,
                    context=context,
                    conversation_so_far=self.conversation,
                    questions_for_me=questions_for_me,
                )
                round_responses[agent_id] = response
                self.conversation.append(ConversationMessage(
                    agent=agent_id, phase=phase_name, content=response,
                ))
                self._log_agent_response(agent_id, agents, phase_name, response)

            all_satisfied = all(
                r.get("satisfied", False) for r in round_responses.values()
            )

            if all_satisfied:
                self._log(f"\n✅ All agents satisfied after {qa_round} Q&A round(s)")

        if not all_satisfied and qa_round >= self.max_qa_rounds:
            self._log(f"\n⚠️ Max Q&A rounds reached with unresolved questions")

        # =================================================================
        # PHASE 3: FINAL VOTE
        # =================================================================
        self._log("\n" + "=" * 70)
        self._log("PHASE 3: FINAL VOTE")
        self._log("=" * 70)

        votes = {}
        vote_responses = {}
        for agent_id in agents:
            response = self._call_agent(
                agent_id=agent_id,
                agents=agents,
                phase="vote",
                context=context,
                conversation_so_far=self.conversation,
            )
            vote_responses[agent_id] = response
            self.conversation.append(ConversationMessage(
                agent=agent_id, phase="vote", content=response,
            ))
            votes[agent_id] = response.get("vote", "reject")
            self._log_agent_response(agent_id, agents, "vote", response)

        # =================================================================
        # BUILD RESULT
        # =================================================================
        vote_counts = {}
        for v in votes.values():
            vote_counts[v] = vote_counts.get(v, 0) + 1

        consensus_reached = vote_counts.get("approve", 0) >= 2

        # Unresolved concerns
        unresolved = []
        for agent_id, resp in vote_responses.items():
            concerns = resp.get("remaining_concerns", [])
            if concerns:
                name = agents[agent_id]["name"]
                for c in concerns:
                    unresolved.append(f"{name}: {c}")

        confidences = [
            r.get("confidence", 0) for r in vote_responses.values()
            if r.get("confidence", 0) > 0
        ]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        self._log(f"\n{'=' * 70}")
        self._log(f"VOTES: {votes}")
        self._log(f"CONSENSUS: {'✅ YES' if consensus_reached else '❌ NO'}")
        self._log(f"Q&A ROUNDS: {qa_round}, ALL RESOLVED: {all_satisfied}")

        # Build result based on mode
        result = ConsensusResult(
            mode=mode,
            fingerprint=fingerprint,
            consensus_reached=consensus_reached,
            vote_summary=vote_counts,
            confidence=avg_confidence,
            qa_rounds_completed=qa_round,
            all_questions_resolved=all_satisfied,
            unresolved_concerns=unresolved,
            conversation_history=[asdict(m) for m in self.conversation],
            debate_transcript="\n".join(self.transcript_lines),
        )

        if mode == "error":
            self._populate_error_result(result, vote_responses, votes, agents)
        elif mode == "feature":
            self._populate_feature_result(result, vote_responses, votes, agents, context)

        return result

    def _populate_error_result(self, result, vote_responses, votes, agents):
        """Fill in error-specific fields."""
        engineer = vote_responses.get("engineer", {})
        diagnostician = vote_responses.get("diagnostician", {})
        reviewer = vote_responses.get("reviewer", {})

        result.final_diagnosis = diagnostician.get("analysis", "")
        result.final_fix = engineer.get("proposed_fix", "")
        result.final_fix_file = engineer.get("fix_file", "")
        result.final_fix_diff = engineer.get("fix_diff", "")
        result.severity = diagnostician.get("severity", "medium")
        result.auto_fixable = (
            result.consensus_reached
            and votes.get("reviewer") == "approve"
            and reviewer.get("safe_to_auto_fix", False)
        )

    def _populate_feature_result(self, result, vote_responses, votes, agents, context):
        """Fill in feature-specific fields."""
        architect = vote_responses.get("architect", {})
        engineer = vote_responses.get("engineer", {})
        qa = vote_responses.get("qa", {})

        result.feature_name = context.get("feature_name", "")
        result.feature_description = context.get("feature_description", "")
        result.architecture = architect.get("analysis", "")
        result.implementation_plan = engineer.get("analysis", "")
        result.implementation_steps = engineer.get("implementation_steps", [])
        result.files_to_change = engineer.get("files_to_change", [])
        result.new_files = engineer.get("new_files", [])
        result.effort_estimate = engineer.get("effort_estimate", "")
        result.complexity = architect.get("estimated_complexity", "")
        result.risk_level = qa.get("risk_level", "")
        result.testing_strategy = qa.get("testing_strategy", "")
        result.rollback_plan = qa.get("rollback_plan", "")
        result.edge_cases = qa.get("edge_cases", [])

    # -------------------------------------------------------------------------
    # Q&A TRACKING
    # -------------------------------------------------------------------------

    def _collect_pending_questions(self, agents) -> Dict[str, List[dict]]:
        """Find unanswered questions in the conversation."""

        all_questions = []
        for i, msg in enumerate(self.conversation):
            q_for = msg.content.get("questions_for", {})
            for target_agent, questions in q_for.items():
                if isinstance(questions, list):
                    for q in questions:
                        if q and q.strip():
                            all_questions.append((msg.agent, target_agent, q, i))

        answered = set()
        for msg in self.conversation:
            answers = msg.content.get("answers", {})
            if isinstance(answers, dict):
                for q_text in answers.keys():
                    for (from_a, to_a, q, idx) in all_questions:
                        if to_a == msg.agent:
                            q_lower = q.lower()[:50]
                            a_lower = q_text.lower()[:50]
                            if q_lower[:30] in a_lower or a_lower[:30] in q_lower:
                                answered.add((from_a, to_a, q))

        pending = {}
        for (from_a, to_a, question, idx) in all_questions:
            if (from_a, to_a, question) not in answered:
                if to_a not in pending:
                    pending[to_a] = []
                from_name = agents.get(from_a, {}).get("name", from_a)
                pending[to_a].append({
                    "from": from_a,
                    "from_name": from_name,
                    "question": question,
                })

        return pending

    # -------------------------------------------------------------------------
    # AGENT COMMUNICATION
    # -------------------------------------------------------------------------

    def _call_agent(self, agent_id, agents, phase, context,
                    conversation_so_far, questions_for_me=None) -> dict:

        profile = agents[agent_id]
        prompt = self._build_prompt(
            agent_id=agent_id,
            agents=agents,
            phase=phase,
            context=context,
            conversation_so_far=conversation_so_far,
            questions_for_me=questions_for_me or [],
        )

        try:
            cmd = [
                "claude", "-p", prompt,
                "--model", self.model,
                "--output-format", "text",
                "--system-prompt", profile["system_prompt"],
            ]
            env = {
                **os.environ,
                "CI": "true",
                "TERM": "dumb",
                "HOME": config.VM_HOME,
                "USER": config.VM_USER,
            }

            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                env=env,
            )

            if result.returncode != 0:
                logger.error(f"Agent {agent_id} CLI error: {result.stderr[:300]}")
                return {"analysis": "Error calling agent", "suggestions": [], "concerns": [], "confidence": 0.0}

            text = result.stdout.strip()

            try:
                return json.loads(text)
            except (json.JSONDecodeError, AttributeError):
                return {
                    "analysis": text[:1000],
                    "suggestions": [],
                    "concerns": [],
                    "confidence": 0.5,
                }

        except subprocess.TimeoutExpired:
            logger.error(f"Agent {agent_id} timed out")
            return {"analysis": "Timed out", "suggestions": [], "concerns": [], "confidence": 0.0}
        except Exception as e:
            logger.error(f"Agent {agent_id} failed: {e}")
            return {"analysis": str(e), "suggestions": [], "concerns": [], "confidence": 0.0}

    def _build_prompt(self, agent_id, agents, phase, context,
                      conversation_so_far, questions_for_me=None) -> str:

        mode = context.get("mode", "error")

        # Build context block based on mode
        if mode == "error":
            context_block = f"""ERROR TYPE: {context['error_type']}
ERROR MESSAGE: {context['error_message']}
SERVICE: {context.get('service', '')}
ENDPOINT: {context.get('endpoint', '')}

TRACEBACK:
{context['traceback']}

SOURCE CODE:
{context['source_context']}"""
        else:
            reqs = "\n".join(f"  - {r}" for r in context.get("requirements", []))
            constraints = "\n".join(f"  - {c}" for c in context.get("constraints", []))
            related = "\n".join(f"  - {f}" for f in context.get("related_files", []))

            context_block = f"""FEATURE: {context['feature_name']}
DESCRIPTION: {context['feature_description']}

REQUIREMENTS:
{reqs or '  (none specified)'}

CONSTRAINTS:
{constraints or '  (none specified)'}

RELATED FILES:
{related or '  (none specified)'}

EXISTING CODE CONTEXT:
{context.get('existing_code_context', '(none provided)')}"""

        mode_label = "ERROR TRIAGE" if mode == "error" else "FEATURE PLANNING"

        if phase == "analysis":
            return f"""PHASE 1: INDEPENDENT ANALYSIS — {mode_label}
Analyze this {'error' if mode == 'error' else 'feature request'}. Give your initial assessment.
Ask questions of the other agents if you need information.

{context_block}

Respond in the JSON format specified in your system prompt."""

        elif phase.startswith("qa_round"):
            round_num = phase.split("_")[-1]
            history = self._format_conversation(conversation_so_far, agents, agent_id)

            questions_section = ""
            if questions_for_me:
                questions_section = "\n⚠️ QUESTIONS YOU MUST ANSWER:\n"
                for q in questions_for_me:
                    questions_section += f"\n  From {q['from_name']}: {q['question']}"
                questions_section += "\n\nAnswer EACH question thoroughly in your 'answers' field.\n"

            return f"""Q&A ROUND {round_num} — {mode_label}

ORIGINAL {'ERROR' if mode == 'error' else 'FEATURE REQUEST'}:
{context_block}

CONVERSATION SO FAR:
{history}
{questions_section}
Now respond:
1. ANSWER any questions directed at you
2. ASK new questions if you have concerns
3. UPDATE your analysis based on what you've learned
4. Set "satisfied" to true if all your concerns are addressed

Respond in the JSON format specified in your system prompt."""

        elif phase == "vote":
            history = self._format_conversation(conversation_so_far, agents, agent_id)

            return f"""PHASE 3: FINAL VOTE — {mode_label}

ORIGINAL {'ERROR' if mode == 'error' else 'FEATURE REQUEST'}:
{context_block}

FULL DISCUSSION:
{history}

Cast your FINAL VOTE. Add "vote" to your response:
- "approve" — confident the {'fix' if mode == 'error' else 'plan'} is correct and safe
- "reject" — the {'fix' if mode == 'error' else 'plan'} is wrong, incomplete, or risky
- "needs_discussion" — not confident either way

Consider whether all your questions were answered satisfactorily.

Respond in the JSON format specified in your system prompt, AND add:
  "vote": "approve|reject|needs_discussion"
"""

        return ""

    def _format_conversation(self, conversation, agents, current_agent) -> str:
        if not conversation:
            return "(No conversation yet)"

        parts = []
        for msg in conversation:
            profile = agents.get(msg.agent, {})
            emoji = profile.get("emoji", "❓")
            name = profile.get("name", msg.agent)
            is_me = " (YOU)" if msg.agent == current_agent else ""
            content = msg.content if isinstance(msg, ConversationMessage) else msg.get("content", {})

            entry = f"\n{'─' * 50}\n{emoji} {name}{is_me} [{msg.phase}]:\n"

            for key in ["analysis", "proposed_fix", "data_model", "api_design",
                        "implementation_steps", "testing_strategy", "rollback_plan",
                        "edge_cases", "fix_diff", "code_sketch"]:
                val = content.get(key)
                if val:
                    if isinstance(val, list):
                        entry += f"  {key}:\n"
                        for item in val:
                            entry += f"    - {item}\n"
                    else:
                        entry += f"  {key}: {val}\n"

            questions = content.get("questions_for", {})
            if questions:
                for target, qs in questions.items():
                    if isinstance(qs, list):
                        for q in qs:
                            t_name = agents.get(target, {}).get("name", target)
                            entry += f"  ❓ → {t_name}: {q}\n"

            answers = content.get("answers", {})
            if answers:
                for q, a in answers.items():
                    entry += f"  💬 Q: {q[:100]}\n     A: {a[:300]}\n"

            concerns = content.get("remaining_concerns", [])
            if concerns:
                entry += f"  ⚠️ Concerns: {concerns}\n"

            satisfied = content.get("satisfied")
            if satisfied is not None:
                entry += f"  {'✅ Satisfied' if satisfied else '❌ Not satisfied'}\n"

            vote = content.get("vote")
            if vote:
                entry += f"  🗳️ Vote: {vote}\n"

            parts.append(entry)

        return "".join(parts)

    # -------------------------------------------------------------------------
    # LOGGING
    # -------------------------------------------------------------------------

    def _log(self, message):
        self.transcript_lines.append(message)
        logger.info(message)

    def _log_agent_response(self, agent_id, agents, phase, response):
        profile = agents[agent_id]
        self._log(f"\n{profile['emoji']} {profile['name']} [{phase}]:")

        for key in ["analysis", "proposed_fix", "data_model", "implementation_steps"]:
            val = response.get(key)
            if val:
                display = val if isinstance(val, str) else json.dumps(val)
                self._log(f"  {key}: {display[:300]}")

        for target, qs in response.get("questions_for", {}).items():
            if isinstance(qs, list):
                for q in qs:
                    t_name = agents.get(target, {}).get("name", target)
                    self._log(f"  ❓ → {t_name}: {q}")

        for q, a in response.get("answers", {}).items():
            self._log(f"  💬 Q: {q[:80]}")
            self._log(f"     A: {a[:200]}")

        if response.get("remaining_concerns"):
            self._log(f"  ⚠️ Concerns: {response['remaining_concerns']}")

        satisfied = response.get("satisfied")
        if satisfied is not None:
            self._log(f"  {'✅ Satisfied' if satisfied else '❌ Not satisfied'}")

        if response.get("vote"):
            self._log(f"  🗳️ Vote: {response['vote']}")
