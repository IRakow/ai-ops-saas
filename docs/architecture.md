# Architecture

## System Overview

AI Ops Debugger is a multi-agent system that uses teams of Claude Opus agents to automatically investigate, fix, test, and deploy changes to your codebase. It consists of three main components:

1. **Web UI** (Flask) — Where users report bugs and monitor progress
2. **Worker daemon** — Polls Supabase for queued tasks and runs agents
3. **Agent pipeline** — The actual Claude agents that do the work

## Agent Pipeline

### Understanding Phase

When a bug is reported, 5 specialist agents analyze it in parallel:

| Agent | Focus | What It Looks For |
|-------|-------|-------------------|
| Error Analyst | Stack traces, logs | Root cause in error output |
| Code Archaeologist | Git history, recent changes | What changed that might have caused this |
| Database Inspector | Schema, queries, data | Database-level issues |
| UX Flow Mapper | Routes, templates, JS | User-facing flow breakdowns |
| Dependency Auditor | Requirements, imports | Dependency conflicts or version issues |

Each specialist runs in read-only mode with 50 turns and a 600-second timeout.

A **Consolidator** then synthesizes all 5 reports into a single technical analysis plus a user-facing summary.

### Execution Phase

1. **Implementer** (150 turns, full write access) — Writes the actual fix using the understanding phase output as context
2. **Regression Tester** + **Supabase Validator** (parallel, read-only) — Verify the fix doesn't break anything
3. **Fixer** (conditional, 80 turns) — If tests fail, gets the test output and tries a different approach
4. **Browser Smoke Test** (Playwright, deterministic) — Runs scripted browser checks
5. **Browser Tester** (Playwright MCP, exploratory) — Claude-driven browser exploration
6. **Final Assessor** — Evaluates all evidence and issues a verdict:
   - **FIXED** — Issue resolved, deploy
   - **PARTIAL** — Some improvement, retry with guidance
   - **FAILED** — No improvement, retry with different approach
   - **REGRESSION** — Made things worse, revert

### Auto-Retry

PARTIAL and FAILED verdicts get re-queued with "try a different approach" instructions. The system retries up to 2 times, passing previous attempt context so agents don't repeat failed approaches.

### Deployment

On a FIXED verdict:
1. Commit changes to git
2. Push to GitHub
3. SSH to production VM and run deploy script
4. Start soak monitor (5-15 min of error log monitoring)
5. If soak finds regressions, alert immediately

## Consensus Engine (Feature Planning)

For feature requests, 3 Claude Opus agents debate before any code is written:

**Error Triage:** Diagnostician + Engineer + Reviewer
**Feature Planning:** Architect + Engineer + QA Agent

They go through up to 4 Q&A rounds where agents question each other's proposals. A 2/3 majority vote is required to proceed.

## Data Flow

```
Supabase Tables:
  ai_ops_users       — Who can use the system
  ai_ops_sessions    — One per bug/feature (tracks full lifecycle)
  ai_ops_messages    — Chat log within each session
  ai_ops_agent_queue — Work items for the worker daemon
  ai_ops_tasks       — Sub-tasks within a session
  ai_ops_files       — Files modified by agents
  ai_ops_fix_patterns — Knowledge base of what worked/failed
  ai_ops_notes       — User feedback and observations
  ai_ops_audit_log   — Everything that happened
```

## Worker Daemon

The worker (`worker.py`) is a long-running Python process that:

1. Polls `ai_ops_agent_queue` every N seconds for pending work
2. Picks up tasks in priority order
3. Runs the appropriate agent pipeline phase
4. Updates session status and messages in Supabase
5. Queues the next phase or triggers deploy

It calls Claude Code via the CLI (`claude --print`) with subprocess, passing system prompts and codebase context. Key environment variables for subprocess:
- `CI=true` — Prevents interactive prompts
- `TERM=dumb` — Prevents terminal escape codes
- `stdin=subprocess.DEVNULL` — Prevents stdin hangs

## Safety Guardrails

- **Blast radius config** — JSON file that limits which files each module can edit
- **Read-only agents** — Specialists, testers, validators can't modify files
- **Soak monitoring** — Post-deploy error log monitoring catches regressions
- **Fix memory** — Records failed approaches so they aren't repeated
- **Human approval** — Feature plans require human sign-off before implementation
