#!/usr/bin/env python3
"""
AI Ops Worker — Background daemon that polls Supabase for pending agent tasks
and drives them through the Claude Code CLI pipeline with gated progress streaming.

Features:
- Line-by-line output streaming with gate detection
- Real-time progress messages to Supabase (for web UI polling)
- Post-deploy soak checks with automatic rollback
- Stuck task recovery on startup
- SMS/email notifications
- Fix memory logging on success
- Graceful shutdown via SIGTERM/SIGINT

Usage:
    python worker.py

All paths, app identity, and deploy targets are configured via config.py and .env.
"""

import os
import re
import sys
import json
import time
import select
import signal
import logging
import subprocess
import threading
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Path setup — must happen before any app imports
# ---------------------------------------------------------------------------

backend_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, backend_dir)
sys.path.insert(0, os.path.join(backend_dir, ".."))

# Load .env BEFORE any imports that read os.environ
from dotenv import load_dotenv
load_dotenv(os.path.join(backend_dir, ".env"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("ai_ops.worker")

# ---------------------------------------------------------------------------
# App imports (after dotenv and path setup)
# ---------------------------------------------------------------------------

from contextlib import contextmanager

from app.services.ai_ops_service import AIOpsService
from app.tenant import load_tenant, TenantConfig
from app.services.git_service import sync_workspace, commit_and_push
from app.services.usage_service import start_usage_record, complete_usage_record, fail_usage_record, check_limits
from app.services.webhook_service import deliver_event
from app.supabase_client import get_supabase_client
import config

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = config.POLL_INTERVAL
AGENT_TIMEOUT = config.AGENT_TIMEOUT
STUCK_TASK_MINUTES = 45
APP_BASE_URL = config.APP_BASE_URL

# ---------------------------------------------------------------------------
# Multi-Agent Configuration
# ---------------------------------------------------------------------------

AGENT_MODEL = "claude-opus-4-6"          # ALL agents — no Sonnet

# Understanding phase (5 parallel specialists + 1 consolidator)
SPECIALIST_TIMEOUT = 600                  # 10 min each (2-at-a-time on 4 vCPU, Opus needs more time)
SPECIALIST_MAX_TURNS = 50                 # Was 25 — agents hit max turns producing 0 output
SPECIALIST_PARALLEL_BATCH = 3             # 3 at a time — 5 specialists in 2 batches (3+2)
CONSOLIDATOR_TIMEOUT = 420                # 7 min
CONSOLIDATOR_MAX_TURNS = 40              # Was 20

# Execution phase (sequential pipeline)
IMPLEMENTER_TIMEOUT = 2400                # 40 min (complex multi-file fixes; was 1800 — timed out on reports bug)
IMPLEMENTER_MAX_TURNS = 150               # Was 120 — cash flow property bug exhausted 120 turns
TESTER_TIMEOUT = 600                      # 10 min (Opus agents need more time for thorough testing)
TESTER_MAX_TURNS = 50                     # Was 30 — agents hit max turns
SUPABASE_VALIDATOR_TIMEOUT = 420          # 7 min (was 3 min — timed out)
SUPABASE_VALIDATOR_MAX_TURNS = 40         # Was 20
ASSESSOR_TIMEOUT = 420                    # 7 min
ASSESSOR_MAX_TURNS = 40                   # Was 15 — agents hit max turns
FIXER_TIMEOUT = 1200                      # 20 min (was 15)
FIXER_MAX_TURNS = 80                      # Was 50 — complex regressions need more

# Browser Tester agent (claude --print with Playwright MCP)
BROWSER_TESTER_TIMEOUT = 300              # 5 min
BROWSER_TESTER_MAX_TURNS = 30

# Smart soak periods
SOAK_NORMAL_SECONDS = 300                 # 5 min for non-sensitive changes
SOAK_SENSITIVE_SECONDS = 900              # 15 min for sensitive file changes
SOAK_MONITOR_INTERVAL = 30               # Check error log every 30s during soak
SENSITIVE_FILE_PATTERNS = [
    "auth.py", "payments.py", "bank_accounts.py", "tenant_portal.py",
    "owner_portal.py", "settings.py", "esignature.py",
    "migration", ".sql", "schema",
]

# Production deploy
PRODUCTION_VM = config.PRODUCTION_VM
PRODUCTION_VM_ZONE = config.PRODUCTION_VM_ZONE
PRODUCTION_DEPLOY_SCRIPT = config.PRODUCTION_DEPLOY_SCRIPT
PRODUCTION_BASE_URL = config.PRODUCTION_BASE_URL

# Global
MULTI_AGENT_TOTAL_TIMEOUT = 7200          # 120 min (was 90 — accounts for soak + production deploy)
MAX_FIX_RETRY_CYCLES = 1

MAX_TURNS_ERROR_PATTERN = re.compile(r"Error:\s*Reached max turns", re.IGNORECASE)

PROTOCOL_FILE = config.PROTOCOL_FILE
TOOLS_DIR = config.TOOLS_DIR
WORKING_DIR = config.WORKING_DIR

VALIDATE_SCRIPT = os.path.join(TOOLS_DIR, config.VALIDATE_SCRIPT) if config.VALIDATE_SCRIPT else ""
GIT_GUARD_SCRIPT = os.path.join(TOOLS_DIR, config.GIT_GUARD_SCRIPT) if config.GIT_GUARD_SCRIPT else ""
BROWSER_SMOKE_SCRIPT = os.path.join(TOOLS_DIR, config.BROWSER_SMOKE_SCRIPT)

# Add tools dir to path once for fix_memory imports
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

SOAK_CHECK_EMAIL = config.SOAK_CHECK_EMAIL
SOAK_CHECK_PASSWORD = config.SOAK_CHECK_PASSWORD

HEARTBEAT_FILE = "/tmp/ai_ops_worker_heartbeat"

# SMS/Notification config
TWILIO_SID = config.TWILIO_ACCOUNT_SID
TWILIO_TOKEN = config.TWILIO_AUTH_TOKEN
TWILIO_FROM = config.TWILIO_PHONE_NUMBER
NOTIFY_PHONES = config.NOTIFICATION_PHONES

DEFAULT_EMAILS = ",".join(config.NOTIFICATION_EMAILS) if config.NOTIFICATION_EMAILS else ""
NOTIFY_EMAILS = config.NOTIFICATION_EMAILS

# ---------------------------------------------------------------------------
# Multi-Tenant Context
# ---------------------------------------------------------------------------


@contextmanager
def tenant_context(tenant: TenantConfig):
    """Set up environment for a specific tenant's agent run."""
    original_env = os.environ.copy()
    try:
        os.environ["WORKING_DIR"] = tenant.workspace_path
        os.environ["APP_NAME"] = tenant.app_name or tenant.name
        os.environ["APP_BASE_URL"] = tenant.app_url or ""
        yield tenant
    finally:
        os.environ.clear()
        os.environ.update(original_env)


def _poll_queue_fair():
    """Pick next task, rotating between tenants."""
    sb = get_supabase_client()
    tasks = sb.table("ai_ops_agent_queue") \
        .select("*, tenants!inner(status, slug, workspace_path)") \
        .eq("status", "pending") \
        .eq("tenants.status", "active") \
        .order("created_at") \
        .limit(1) \
        .execute()

    if not tasks.data:
        # Also check trial tenants
        tasks = sb.table("ai_ops_agent_queue") \
            .select("*, tenants!inner(status, slug)") \
            .eq("status", "pending") \
            .eq("tenants.status", "trial") \
            .order("created_at") \
            .limit(1) \
            .execute()

    return tasks.data[0] if tasks.data else None


# Gate detection patterns
GATE_PATTERNS = [
    (re.compile(r"GATE\s+1:", re.IGNORECASE), "gate_1", "Gate 1: Reproduce/Understand"),
    (re.compile(r"GATE\s+2:", re.IGNORECASE), "gate_2", "Gate 2: Trace/Implement"),
    (re.compile(r"GATE\s+3:", re.IGNORECASE), "gate_3", "Gate 3: Fix/Verify"),
    (re.compile(r"GATE\s+4:", re.IGNORECASE), "gate_4", "Gate 4: Validate"),
    (re.compile(r"GATE\s+5:", re.IGNORECASE), "gate_5", "Gate 5: Report"),
]

ESCALATION_PATTERN = re.compile(r"ESCALATION:", re.IGNORECASE)
VERIFIED_FIXED_PATTERN = re.compile(r"VERIFIED FIXED", re.IGNORECASE)
SMOKE_PASS_PATTERN = re.compile(r"ALL SMOKE TESTS PASS", re.IGNORECASE)
ROLLBACK_OK_PATTERN = re.compile(r"ROLLBACK OK", re.IGNORECASE)
COMMIT_PATTERN = re.compile(r"^COMMIT:\s*(.+)", re.MULTILINE)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

shutdown = False


def handle_signal(signum, frame):
    global shutdown
    log.info("Received signal %s, initiating shutdown...", signum)
    shutdown = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ---------------------------------------------------------------------------
# SMS Notification
# ---------------------------------------------------------------------------


def _send_sms_sync(message):
    """Send SMS to all notification recipients via Twilio (synchronous)."""
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        for phone in NOTIFY_PHONES:
            phone = phone.strip()
            if not phone:
                continue
            try:
                msg = client.messages.create(
                    body=message, from_=TWILIO_FROM, to=phone
                )
                log.info("SMS sent to %s: %s", phone, msg.sid)
            except Exception as e:
                log.error("SMS to %s failed: %s", phone, e)
    except ImportError:
        log.warning("twilio package not installed, SMS skipped")
    except Exception as e:
        log.error("SMS failed: %s", e)


def send_sms(message):
    """Send SMS in a background thread so it doesn't block the pipeline."""
    if not TWILIO_SID or not TWILIO_TOKEN:
        log.warning("Twilio not configured, SMS skipped: %s", message[:120])
        return

    t = threading.Thread(target=_send_sms_sync, args=(message,), daemon=True)
    t.start()


def send_email(subject, body):
    """Send email to all notification recipients via SendGrid."""
    sendgrid_key = config.SENDGRID_API_KEY
    if not sendgrid_key:
        log.warning("SendGrid not configured. Subject: %s", subject)
        return

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        sg = SendGridAPIClient(sendgrid_key)
        from_email = config.NOTIFICATION_FROM_EMAIL

        for email in NOTIFY_EMAILS:
            email = email.strip()
            if not email:
                continue
            try:
                message = Mail(
                    from_email=from_email,
                    to_emails=email,
                    subject=subject,
                    plain_text_content=body,
                )
                resp = sg.send(message)
                log.info("Email sent to %s: status %s", email, resp.status_code)
            except Exception as e:
                log.error("Email to %s failed: %s", email, e)
    except ImportError:
        log.warning("sendgrid package not installed, email skipped")
    except Exception as e:
        log.error("Email failed: %s", e)


# ---------------------------------------------------------------------------
# Bug Intake — Queue checker + AI Ops session creator
# ---------------------------------------------------------------------------

_BUG_SYSTEM_USER_EMAIL = config.SYSTEM_USER_EMAIL
_BUG_SYSTEM_USER_ID = None  # Cached after first lookup/create
BUG_CHECK_INTERVAL = 3      # Check bug queue every Nth poll cycle
GITHUB_TOKEN = config.GITHUB_TOKEN


def _get_or_create_system_user(svc):
    """Get or create the system AI Ops user for bug intake sessions."""
    global _BUG_SYSTEM_USER_ID
    if _BUG_SYSTEM_USER_ID:
        return _BUG_SYSTEM_USER_ID

    result = (
        svc.supabase.table("ai_ops_users")
        .select("id")
        .eq("email", _BUG_SYSTEM_USER_EMAIL)
        .limit(1)
        .execute()
    )
    if result.data:
        _BUG_SYSTEM_USER_ID = result.data[0]["id"]
        return _BUG_SYSTEM_USER_ID

    # Create system user (password_hash required by schema — use non-loginable hash)
    import hashlib as _hl
    _dummy_hash = _hl.sha256(b"SYSTEM_NO_LOGIN").hexdigest()
    result = svc.supabase.table("ai_ops_users").insert({
        "email": _BUG_SYSTEM_USER_EMAIL,
        "name": "Bug Intake System",
        "password_hash": _dummy_hash,
        "role": "admin",
        "is_active": True,
    }).execute()
    if result.data:
        _BUG_SYSTEM_USER_ID = result.data[0]["id"]
    return _BUG_SYSTEM_USER_ID


def check_bug_queue(svc):
    """Check for new bug reports and create AI Ops sessions for them."""
    try:
        from app.services.bug_intake_service import BugIntakeService
        bug_svc = BugIntakeService()
        reports = bug_svc.get_new_reports(limit=3)

        if not reports:
            return

        log.info("Bug intake: found %d new bug report(s)", len(reports))

        for report in reports:
            try:
                _create_ai_ops_session_for_bug(svc, bug_svc, report)
            except Exception as e:
                log.error("Bug intake: failed to process bug %s: %s", report["id"], e)
                bug_svc.update_status(
                    report["id"], "failed",
                    "Failed to create AI Ops session: {err}".format(err=str(e)[:200]),
                )
    except Exception as e:
        log.error("Bug intake queue check failed: %s", e, exc_info=True)


def _create_ai_ops_session_for_bug(svc, bug_svc, report):
    """Create an AI Ops session and queue task for a bug report or feature request."""
    bug_id = report["id"]
    source = report.get("source", "auto_detect")
    error_type = report.get("error_type", "unknown")
    url_path = report.get("url_path", "/unknown")
    error_msg = report.get("error_message", "No error message")
    environment = report.get("environment", "test")

    # Mark as queued immediately to prevent re-pickup
    bug_svc.update_status(bug_id, "queued", "Creating AI Ops session...")

    # Get system user
    user_id = _get_or_create_system_user(svc)
    if not user_id:
        raise RuntimeError("Could not create system user for bug intake")

    # Branch on source type
    if source == "feature_request":
        return _create_feature_request_session(svc, bug_svc, report, user_id)

    # --- Bug flow (existing) ---
    title = "Auto Bug: {etype} on {path}".format(
        etype=error_type, path=url_path[:80],
    )
    session = svc.create_session(user_id, mode="bug_fix", title=title)
    if not session:
        raise RuntimeError("Failed to create AI Ops session")

    session_id = session["id"]

    # Build rich description from bug context
    desc_parts = [
        "## Auto-Detected Bug Report",
        "",
        "**Error type:** {t}".format(t=error_type),
        "**URL path:** {p}".format(p=url_path),
        "**Environment:** {e}".format(e=environment),
        "**Error message:**",
        "```",
        error_msg[:3000],
        "```",
    ]

    if report.get("js_stack_trace"):
        desc_parts.extend([
            "",
            "**Stack trace:**",
            "```",
            report["js_stack_trace"][:5000],
            "```",
        ])

    if report.get("user_description"):
        desc_parts.extend([
            "",
            "**User description:** {d}".format(d=report["user_description"]),
        ])

    if report.get("console_log_tail"):
        desc_parts.extend([
            "",
            "**Console log (last entries):**",
            "```json",
            json.dumps(report["console_log_tail"], indent=2)[:2000],
            "```",
        ])

    if report.get("network_errors"):
        desc_parts.extend([
            "",
            "**Network errors:**",
            "```json",
            json.dumps(report["network_errors"], indent=2)[:2000],
            "```",
        ])

    if report.get("screenshot_gcs_url"):
        desc_parts.extend([
            "",
            "**Screenshot:** {url}".format(url=report["screenshot_gcs_url"]),
        ])

    if report.get("user_agent"):
        desc_parts.append("")
        desc_parts.append("**User agent:** {ua}".format(ua=report["user_agent"]))

    if report.get("page_html_snippet"):
        desc_parts.extend([
            "",
            "**Page context:** {s}".format(s=report["page_html_snippet"][:500]),
        ])

    description = "\n".join(desc_parts)

    # Build attachments list
    attachments = []
    if report.get("screenshot_gcs_url"):
        attachments.append({
            "file_name": "bug-screenshot.png",
            "file_url": report["screenshot_gcs_url"],
            "file_path": report.get("screenshot_gcs_path", ""),
        })

    # Queue task with understand phase (full pipeline)
    queue_item = svc.queue_task(
        session_id,
        task_type="bug",
        description=description,
        attachments=attachments,
        phase="understand",
    )

    queue_id = queue_item["id"] if queue_item else None

    # Update session status to queued (must match what worker expects)
    svc.update_session(session_id, status="queued", task_type="bug")

    # Link bug report to AI Ops session
    bug_svc.link_to_ai_ops(bug_id, session_id, queue_id)

    # Add initial message
    svc.add_message(
        session_id, "system", "Bug Intake",
        "Auto-detected bug report created from {src}.\n\n{desc}".format(
            src=source,
            desc=description[:2000],
        ),
        message_type="status_update",
    )

    # SMS notification
    send_sms(
        f"{config.APP_NAME} Bug Intake: New {error_type} on {url_path[:60]} ({environment}). "
        "AI Ops session created."
    )

    log.info(
        "Bug intake: created AI Ops session %s for bug %s (%s on %s)",
        session_id, bug_id, error_type, url_path,
    )


def _create_feature_request_session(svc, bug_svc, report, user_id):
    """Create an AI Ops session for a feature request with GitHub pre-check."""
    bug_id = report["id"]
    user_desc = report.get("user_description") or report.get("error_message", "Feature request")
    url_path = report.get("url_path", "/unknown")
    environment = report.get("environment", "test")

    title = "Feature Request: {desc}".format(desc=user_desc[:80])
    session = svc.create_session(user_id, mode="new_feature", title=title)
    if not session:
        raise RuntimeError("Failed to create AI Ops session for feature request")

    session_id = session["id"]

    # Run GitHub pre-check to find existing implementations
    pre_check_findings = _check_existing_implementation(user_desc)

    # Build description with pre-check findings prepended
    desc_parts = [
        "## Feature Request",
        "",
        "**Requested from page:** {p}".format(p=url_path),
        "**Environment:** {e}".format(e=environment),
        "",
        "**Description:**",
        user_desc[:5000],
    ]

    if pre_check_findings:
        desc_parts = [
            "## Pre-Check: Existing Implementation Search",
            "",
            pre_check_findings,
            "",
            "---",
            "",
        ] + desc_parts

    description = "\n".join(desc_parts)

    # Queue task with understand phase
    queue_item = svc.queue_task(
        session_id,
        task_type="feature",
        description=description,
        attachments=[],
        phase="understand",
    )

    queue_id = queue_item["id"] if queue_item else None

    # Update session status to queued (must match what worker expects)
    svc.update_session(session_id, status="queued", task_type="feature")

    # Link to bug_reports table
    bug_svc.link_to_ai_ops(bug_id, session_id, queue_id)

    # Add initial message
    svc.add_message(
        session_id, "system", "Feature Intake",
        "Feature request submitted.\n\n{desc}".format(desc=description[:3000]),
        message_type="status_update",
    )

    # SMS notification
    send_sms(
        f"{config.APP_NAME}: New feature request from {url_path[:40]}: {user_desc[:60]}"
    )

    log.info(
        "Feature intake: created AI Ops session %s for feature %s",
        session_id, bug_id,
    )


def _check_existing_implementation(feature_description):
    """Search the GitHub repo for existing code related to the feature request.

    Uses claude --print with read-only tools to search the codebase.
    Returns markdown findings or empty string.
    """
    try:
        prompt = (
            "Search this codebase and project history for anything related to the "
            "following feature request. Look for:\n"
            "1. Existing routes, templates, or JS files that partially implement this\n"
            "2. AppFolio screenshots or mockups in docs/ or static/img/ that could "
            "serve as design reference\n"
            "3. Database tables or service methods that relate to this feature\n"
            "4. Any commented-out or disabled code that was a prior attempt\n"
            "5. Supabase table schemas that relate to this feature — run:\n"
            '   bash: python3 -c "from supabase import create_client; import os; '
            "sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY']); "
            "r = sb.table('information_schema.columns').select('table_name,column_name,data_type')"
            ".eq('table_schema','public').execute(); "
            "[print(f'{c[\\\"table_name\\\"]}.{c[\\\"column_name\\\"]} ({c[\\\"data_type\\\"]})') "
            "for c in r.data if any(kw in c['table_name'].lower() for kw in KEYWORDS)]\"\n"
            "   (Replace KEYWORDS with words from the feature description)\n"
            "6. GitHub Issues mentioning this feature — run:\n"
            "   bash: gh issue list --repo {repo} --search 'KEYWORDS' --limit 10 --state all\n"
            "7. GitHub PRs mentioning this feature — run:\n"
            "   bash: gh pr list --repo {repo} --search 'KEYWORDS' --limit 10 --state all\n"
            "\n"
            "Feature request: {desc}\n\n"
            "Return a concise markdown summary organized by section:\n"
            "### Existing Code\n### Database Tables\n### GitHub Issues & PRs\n### Design References\n"
            "If nothing found in a section, say 'None found.'"
        ).format(desc=feature_description[:2000], repo=config.GITHUB_REPO)

        env = os.environ.copy()
        env["CI"] = "true"
        env["TERM"] = "dumb"

        result = subprocess.run(
            [
                "claude", "--print",
                "--model", AGENT_MODEL,
                "--max-turns", "30",
                "--allowedTools", "Bash,Read,Glob,Grep",
                "-p", prompt,
            ],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
            cwd=WORKING_DIR,
            stdin=subprocess.DEVNULL,
            env=env,
        )

        output = (result.stdout or "").strip()

        # Check for max turns error (output is lost)
        if MAX_TURNS_ERROR_PATTERN.search(output):
            log.warning("Pre-check hit max turns")
            return "Pre-check: Agent hit turn limit. Partial search only."

        if output:
            log.info("Pre-check found %d chars of findings", len(output))
            return output

        return ""

    except subprocess.TimeoutExpired:
        log.warning("Pre-check timed out after 10 minutes")
        return "Pre-check: Search timed out."
    except Exception as e:
        log.error("Pre-check failed: %s", e)
        return ""


def _update_bug_status_from_verdict(svc, session_id, verdict):
    """After AI Ops execution, update the linked bug report status."""
    try:
        from app.services.bug_intake_service import BugIntakeService
        bug_svc = BugIntakeService()
        bug = bug_svc.find_bug_by_session(session_id)
        if not bug:
            return

        bug_id = bug["id"]
        verdict_upper = (verdict or "").upper()
        is_feature = bug.get("source") == "feature_request"

        if verdict_upper == "FIXED" or verdict_upper == "PASS":
            if is_feature:
                # Feature requests go to test approval gate — never auto-deploy
                bug_svc.update_status(bug_id, "fixed", "Feature implemented. Awaiting test approval.")
                svc.update_session(session_id, status="awaiting_test_approval")
                svc.add_message(
                    session_id, "system", "Worker",
                    "Feature implementation complete. Please test on the test server "
                    "and approve for production deployment.",
                    message_type="status_update",
                )
                send_sms(
                    f"{config.APP_NAME}: Feature request implemented! Test and approve: "
                    f"{APP_BASE_URL}/ai-ops/session/{session_id}"
                )
            else:
                bug_svc.update_status(bug_id, "fixed", "Bug has been fixed!")
                _trigger_auto_deploy(bug_svc, bug)
        elif verdict_upper == "FAIL" or verdict_upper == "FAILED":
            bug_svc.update_status(bug_id, "failed", "Fix attempt failed. We are investigating.")
        else:
            bug_svc.update_status(bug_id, "escalated", "This needs human attention.")

    except Exception as e:
        log.error("Bug status update failed for session %s: %s", session_id, e)


def _trigger_auto_deploy(bug_svc, bug):
    """After a bug is fixed, trigger deployment."""
    bug_id = bug["id"]
    environment = bug.get("environment", "test")

    try:
        if environment == "test":
            # Test server: commit, push, reload gunicorn
            log.info("Bug %s: auto-deploying on test server", bug_id)
            cmds = [
                "cd {wd} && git add -A && git diff --cached --quiet || "
                "git commit -m 'fix: auto-fix bug {bid}'".format(wd=WORKING_DIR, bid=bug_id[:8]),
                "cd {wd} && git push origin main".format(wd=WORKING_DIR),
            ]
            for cmd in cmds:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=60,
                )
                if result.returncode != 0:
                    log.warning("Deploy cmd failed: %s\n%s", cmd, result.stderr)

            # Graceful reload
            subprocess.run(
                f"kill -HUP $(pgrep -f '{config.GUNICORN_PATTERN}')",
                shell=True, capture_output=True, timeout=10,
            )

            bug_svc.update_status(bug_id, "deployed", "Fix deployed to test server.")
            send_sms(f"{config.APP_NAME}: Bug #{bug_id[:8]} deployed to test.")

        elif environment == "production":
            # Production: trigger GitHub Actions deploy
            if not GITHUB_TOKEN:
                log.warning("Bug %s: GITHUB_TOKEN not set, skipping prod deploy", bug_id)
                send_sms(
                    f"{config.APP_NAME}: Bug #{bug_id[:8]} fixed but GITHUB_TOKEN not set. "
                    "Deploy manually."
                )
                return

            log.info("Bug %s: triggering production deploy via GitHub Actions", bug_id)
            import urllib.request

            url = f"https://api.github.com/repos/{config.GITHUB_REPO}/actions/workflows/{config.DEPLOY_WORKFLOW}/dispatches"
            payload = json.dumps({"ref": "main"}).encode()
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={
                    "Authorization": "Bearer {t}".format(t=GITHUB_TOKEN),
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
            )
            urllib.request.urlopen(req, timeout=30)

            bug_svc.update_status(bug_id, "deployed", "Fix deployed to production.")
            send_sms(f"{config.APP_NAME}: Bug #{bug_id[:8]} deployed to production.")

    except Exception as e:
        log.error("Auto-deploy failed for bug %s: %s", bug_id, e, exc_info=True)
        send_sms(
            f"{config.APP_NAME}: Bug #{bug_id[:8]} auto-deploy FAILED: {str(e)[:100]}"
        )


# ---------------------------------------------------------------------------
# Prompt Building
# ---------------------------------------------------------------------------


def build_prompt(task_type, description, attachments=None, tenant=None):
    """Build the full prompt for the Claude Code agent.

    Args:
        task_type: "bug" or "feature"
        description: The task description from the queue item.
        attachments: List of GCS URLs or file references (from JSONB field).
        tenant: Optional TenantConfig for multi-tenant operation.
    """
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR

    # Read the protocol file
    protocol = ""
    try:
        with open(PROTOCOL_FILE, "r") as f:
            protocol = f.read()
    except Exception as e:
        log.error("Could not read protocol file %s: %s", PROTOCOL_FILE, e)
        protocol = "(Protocol file not available)"

    if task_type == "bug":
        mode = "BUG FIX"
        instruction = "Follow the BUG FIX PROTOCOL in the protocol document below."
    else:
        mode = "NEW FEATURE"
        instruction = "Follow the NEW FEATURE PROTOCOL in the protocol document below."

    # Fix memory context
    fix_context = ""
    try:
        from fix_memory import get_recent_fixes
        fix_context = get_recent_fixes(15)
    except ImportError:
        pass
    except Exception as e:
        log.debug("Fix memory not available: %s", e)

    if fix_context:
        fix_context = (
            "\n## RECENT FIX HISTORY (for context -- do not re-apply these)\n"
            + fix_context
            + "\n"
        )

    attachment_text = _format_attachments(attachments)

    prompt = (
        "## MODE: {mode}\n"
        "\n"
        "## TASK\n"
        "{description}\n"
        "{attachment_text}\n"
        "{fix_context}"
        "\n"
        "## OPERATING PROTOCOL\n"
        "{instruction}\n"
        "\n"
        "{protocol}\n"
        "\n"
        "## FINAL REMINDERS\n"
        "- You are autonomous. Do not ask for approval. Do the work.\n"
        "- After completion, output a structured summary (see Step 5 in the protocol).\n"
        "- If you hit an escalation trigger, output ESCALATION: followed by the reason.\n"
        "- NEVER run git push. Deploy by merging locally on the VM and restarting supervisor.\n"
        "- All work happens locally on this VM. Working directory is {working_dir}.\n"
    ).format(
        mode=mode,
        description=description,
        attachment_text=attachment_text,
        fix_context=fix_context,
        instruction=instruction,
        protocol=protocol,
        working_dir=effective_working_dir,
    )
    return prompt


def _format_attachments(attachments):
    """Format attachment list into text for prompts."""
    if not attachments:
        return ""
    text = "\n\nATTACHED FILES:\n"
    for a in attachments:
        if isinstance(a, dict):
            url = a.get("url", a.get("gcs_url", ""))
            name = a.get("filename", a.get("name", "attachment"))
            text += "- {}: {}\n".format(name, url)
        elif isinstance(a, str):
            text += "- {}\n".format(a)
    return text


def _safe(text):
    """Escape curly braces in external content so it's safe for str.format().

    The protocol file, agent outputs, and user descriptions may contain literal
    {curly_braces} (e.g. {module}, {security}, {token} in code examples).
    Python's str.format() interprets these as named placeholders and throws
    KeyError. Escaping them to {{ and }} prevents this.
    """
    if not text:
        return text or ""
    return text.replace("{", "{{").replace("}", "}}")


# --- Understanding Phase Prompt Builders ---

SPECIALIST_ROLES = {
    "route_tracer": {
        "title": "Route Tracer",
        "instructions": (
            "Your job: trace the HTTP request path from URL to response.\n"
            "1. Find the route file and function that handles the URL mentioned in the task.\n"
            "2. Identify all service calls made from that route function.\n"
            "3. Identify the template rendered and what data is passed to it.\n"
            "4. Check for middleware, decorators, and auth checks on the route.\n"
            "5. Trace the full data flow from request to response.\n"
            "\n"
            "Output format:\n"
            "### ROUTE TRACE\n"
            "- URL: [the URL pattern]\n"
            "- Route file: [path]\n"
            "- Function: [name]\n"
            "- Service calls: [list each service method called]\n"
            "- Template: [template path]\n"
            "- Data passed to template: [list variables]\n"
            "- Auth/decorators: [list]\n"
            "- Data flow: [step-by-step from request to response]\n"
            "\n"
            "### SUSPICIOUS AREAS\n"
            "[List anything that looks wrong, missing, or inconsistent in the route path]\n"
        ),
    },
    "service_analyst": {
        "title": "Service/Logic Analyst",
        "instructions": (
            "Your job: examine the service layer and business logic.\n"
            "1. Find the service classes/methods called by the relevant route.\n"
            "2. Check data transformations — are values being converted correctly?\n"
            "3. Check error handling — are exceptions swallowed or mishandled?\n"
            "4. Look for wrong variable names, typos, logic bugs.\n"
            "5. Check edge cases — what happens with empty data, None values, missing keys?\n"
            "\n"
            "Output format:\n"
            "### SERVICE ANALYSIS\n"
            "- Service files examined: [list with paths]\n"
            "- Methods analyzed: [list each method and what it does]\n"
            "- Data flow through services: [step-by-step]\n"
            "\n"
            "### LOGIC ISSUES FOUND\n"
            "[List each issue with file:line and explanation]\n"
            "\n"
            "### SERVICE RISK AREAS\n"
            "[Areas that could break even if they look OK now]\n"
        ),
    },
    "frontend_inspector": {
        "title": "Frontend/Template Inspector",
        "instructions": (
            "Your job: check Jinja2 templates, JavaScript, and CSS.\n"
            "1. Find the template(s) rendered for this feature.\n"
            "2. Check for wrong variable references — does the template use variables "
            "that the route actually passes?\n"
            "3. Check for broken loops, missing fields, wrong conditionals.\n"
            "4. Check JavaScript files for AJAX errors, wrong endpoints, event handler issues.\n"
            "5. Check form bindings — do form field names match what the backend expects?\n"
            "6. Check CSS for display:none, visibility:hidden, or z-index issues.\n"
            "\n"
            "Output format:\n"
            "### FRONTEND ANALYSIS\n"
            "- Template files: [list with paths]\n"
            "- JS files: [list with paths]\n"
            "- Variables expected by template: [list]\n"
            "- Variables passed by route: [list]\n"
            "- AJAX endpoints called: [list]\n"
            "- Form fields: [list with names and types]\n"
            "\n"
            "### FRONTEND ISSUES FOUND\n"
            "[List each issue with file:line and explanation]\n"
        ),
    },
    "supabase_specialist": {
        "title": "Supabase Specialist",
        "instructions": (
            "Your job: analyze database queries, schemas, and RLS policies.\n"
            "1. Find the Supabase queries related to this feature (search for .table() calls).\n"
            "2. Check that column names in queries match actual table schemas.\n"
            "3. Verify all queries filter by organization_id for multi-tenant isolation.\n"
            "4. Check RLS policies on affected tables.\n"
            "5. Look for missing indexes, broken joins, wrong column references.\n"
            "6. Check if the data the feature needs actually exists — could be empty query results.\n"
            "7. Run read-only SQL queries if needed to check schema:\n"
            "   bash: python3 -c \"...\"\n"
            "\n"
            "Output format:\n"
            "### DATABASE ANALYSIS\n"
            "- Tables involved: [list with column names]\n"
            "- Queries found: [list each query location and what it does]\n"
            "- RLS status: [enabled/disabled for each table]\n"
            "- Organization_id filtering: [present/missing for each query]\n"
            "\n"
            "### DATA ISSUES FOUND\n"
            "[List each issue with file:line and explanation]\n"
            "\n"
            "### SCHEMA RISK AREAS\n"
            "[Missing constraints, indexes, or potential data integrity issues]\n"
        ),
    },
    "security_analyst": {
        "title": "Security Analyst",
        "instructions": (
            "Your job: assess security implications of this bug/feature.\n"
            "1. Check authentication decorators — is @token_required present on all routes involved?\n"
            "2. Check authorization — does the route verify the user has permission for this action?\n"
            "3. Check for SQL injection via raw queries or string formatting in Supabase calls.\n"
            "4. Check for XSS — is user input escaped in templates? Are |safe filters used incorrectly?\n"
            "5. Check CSRF protection — do POST/PUT/DELETE endpoints validate CSRF tokens?\n"
            "6. Check RLS bypass — does any query use the service_role_key when it should use anon key?\n"
            "7. Check for IDOR — can one tenant access another tenant's data by changing an ID in the URL?\n"
            "8. Check file upload handling — are file types validated? Are paths sanitized?\n"
            "\n"
            "Output format:\n"
            "### SECURITY ANALYSIS\n"
            "- Routes examined: [list with auth decorator status]\n"
            "- Input validation: [where user input enters and how it's handled]\n"
            "- RLS/multi-tenant: [org_id filtering status]\n"
            "\n"
            "### SECURITY ISSUES FOUND\n"
            "[List each issue with file:line, severity (Critical/High/Medium/Low), and explanation]\n"
            "\n"
            "### SECURITY RECOMMENDATIONS\n"
            "[Changes needed to maintain security posture]\n"
        ),
    },
}


def build_specialist_prompt(role, task_type, description, attachments=None,
                            tenant=None):
    """Build prompt for an understanding-phase specialist agent."""
    spec = SPECIALIST_ROLES[role]
    mode = "Bug Fix" if task_type == "bug" else "New Feature"
    attachment_text = _format_attachments(attachments)
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR
    effective_context = tenant.get_context() if tenant else config.get_codebase_context()

    return (
        "## MODE: ANALYSIS ONLY — DO NOT MAKE ANY CHANGES\n"
        "## ROLE: {title}\n"
        "## TASK TYPE: {mode}\n"
        "\n"
        "## TASK DESCRIPTION\n"
        "{description}\n"
        "{attachment_text}\n"
        "\n"
        "## YOUR SPECIFIC INSTRUCTIONS\n"
        "You are a specialist analyzing a {mode_lower} request for {app_description}. "
        "The codebase is at {working_dir}/app/.\n"
        "\n"
        "{instructions}\n"
        "\n"
        "CRITICAL RULES:\n"
        "- Do NOT modify any files. Do NOT run git commands. Do NOT restart services.\n"
        "- ONLY read files with Read/Glob/Grep and run read-only bash commands.\n"
        "- Be thorough but concise. Focus on YOUR specialty area.\n"
        "- Working directory: {working_dir}\n"
        "\n"
        "CRITICAL — YOUR OUTPUT WILL BE LOST IF YOU USE ALL YOUR TURNS:\n"
        "You have approximately 50 tool-call turns. If you exhaust ALL turns, your\n"
        "output is replaced with an error message and ALL YOUR ANALYSIS IS LOST.\n"
        "\n"
        "MANDATORY WORKFLOW:\n"
        "1. Spend your FIRST 30 turns investigating (read files, run commands).\n"
        "2. After ~30 tool calls, STOP investigating immediately.\n"
        "3. Write your COMPLETE analysis as TEXT OUTPUT (not to a file).\n"
        "4. Your analysis text IS your deliverable — it must be thorough.\n"
        "\n"
        "EFFICIENCY RULES:\n"
        "- Read only the files directly relevant to your specialty.\n"
        "- Do NOT exhaustively explore every directory — be targeted.\n"
        "- Start with Glob/Grep to locate files, then Read specific sections.\n"
        "- If you cannot find something in 5 searches, move on.\n"
    ).format(
        title=spec["title"],
        mode=mode,
        mode_lower=mode.lower(),
        description=description,
        attachment_text=attachment_text,
        instructions=spec["instructions"],
        app_description=effective_context,
        working_dir=effective_working_dir,
    )


def build_consolidator_prompt(task_type, description, specialist_outputs,
                              tenant=None):
    """Build prompt for the consolidator that merges all specialist outputs."""
    mode = "Bug Fix" if task_type == "bug" else "New Feature"
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR

    separator = "=" * 60
    specialist_text = ""
    for role_name, output in specialist_outputs.items():
        specialist_text += (
            "\n{sep}\n"
            "## SPECIALIST: {role}\n"
            "{sep}\n"
            "{output}\n"
        ).format(sep=separator, role=role_name, output=output)

    return (
        "## MODE: ANALYSIS CONSOLIDATION — DO NOT MAKE ANY CHANGES\n"
        "## ROLE: Consolidator\n"
        "## TASK TYPE: {mode}\n"
        "\n"
        "## TASK DESCRIPTION\n"
        "{description}\n"
        "\n"
        "## SPECIALIST ANALYSIS OUTPUTS\n"
        "Five specialist agents have analyzed this task from different angles. "
        "Their outputs are below.\n"
        "{specialist_text}\n"
        "\n"
        "## YOUR INSTRUCTIONS\n"
        "Cross-reference all specialist findings. Look for:\n"
        "- Agreement — do multiple specialists identify the same issue?\n"
        "- Contradictions — does one specialist's finding conflict with another?\n"
        "- Gaps — did any specialist miss something obvious?\n"
        "\n"
        "You may read code files to verify any specialist's claims.\n"
        "\n"
        "Produce this EXACT output format with TWO sections separated by delimiters:\n"
        "\n"
        "===TECHNICAL_ANALYSIS_START===\n"
        "### What I Understand\n"
        "[Plain English summary of the problem in 2-4 sentences]\n"
        "\n"
        "### Root Cause\n"
        "[Specific root cause with file:line reference]\n"
        "\n"
        "### Files Involved\n"
        "[Every file that needs to change, with brief notes on what changes]\n"
        "\n"
        "### My Approach\n"
        "[Numbered steps with specific details — what to change in each file]\n"
        "\n"
        "### Risk Assessment\n"
        "[Low/Medium/High — and specific concerns]\n"
        "\n"
        "### Regression Check Points\n"
        "[List 5+ related endpoints/features that should be tested after the fix "
        "to make sure nothing else broke. Format: one per line, with URL path "
        "and what to check.]\n"
        "===TECHNICAL_ANALYSIS_END===\n"
        "\n"
        "===USER_SUMMARY_START===\n"
        "[Write a clean 3-5 sentence summary for non-technical users. "
        "Use plain English only. Do NOT include any file paths, code snippets, "
        "function names, database table names, SQL, or architecture details. "
        "Focus on: what the bug/issue is in user-visible terms, what was found, "
        "and what will be fixed or changed.]\n"
        "===USER_SUMMARY_END===\n"
        "\n"
        "CRITICAL RULES:\n"
        "- Do NOT modify any files. Do NOT run git commands. Do NOT restart services.\n"
        "- ONLY read files with Read/Glob/Grep to verify specialist claims.\n"
        "- Working directory: {working_dir}\n"
        "- You MUST include both the ===TECHNICAL_ANALYSIS=== and ===USER_SUMMARY=== "
        "sections with their delimiters.\n"
    ).format(
        mode=mode,
        description=description,
        specialist_text=specialist_text,
        working_dir=effective_working_dir,
    )


# --- Execution Phase Prompt Builders ---


def build_implementer_prompt(task_type, description, attachments, understanding_output,
                             tenant=None):
    """Build prompt for the Implementer agent with full analysis context."""
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR

    # Read the protocol file
    protocol = ""
    try:
        with open(PROTOCOL_FILE, "r") as f:
            protocol = f.read()
    except Exception as e:
        log.error("Could not read protocol file %s: %s", PROTOCOL_FILE, e)
        protocol = "(Protocol file not available)"

    if task_type == "bug":
        mode = "BUG FIX"
        instruction = "Follow the BUG FIX PROTOCOL in the protocol document below."
    else:
        mode = "NEW FEATURE"
        instruction = "Follow the NEW FEATURE PROTOCOL in the protocol document below."

    # Fix memory context
    fix_context = ""
    try:
        from fix_memory import get_recent_fixes
        fix_context = get_recent_fixes(15)
    except ImportError:
        pass
    except Exception as e:
        log.debug("Fix memory not available: %s", e)

    if fix_context:
        fix_context = (
            "\n## RECENT FIX HISTORY (for context -- do not re-apply these)\n"
            + fix_context + "\n"
        )

    attachment_text = _format_attachments(attachments)

    return (
        "## MODE: {mode}\n"
        "\n"
        "## PRIOR ANALYSIS (from specialist team)\n"
        "A team of 5 specialist agents analyzed this task and produced the following "
        "consolidated analysis. Use this as your starting point — you can skip "
        "reproduction and tracing if the analysis looks correct, and jump straight "
        "to implementing the fix (Gate 3). If the analysis seems wrong after your "
        "own investigation, fall back to Gate 1.\n"
        "\n"
        "{understanding}\n"
        "\n"
        "## TASK\n"
        "{description}\n"
        "{attachment_text}\n"
        "{fix_context}"
        "\n"
        "## OPERATING PROTOCOL\n"
        "{instruction}\n"
        "\n"
        "{protocol}\n"
        "\n"
        "## FINAL REMINDERS\n"
        "- You are autonomous. Do not ask for approval. Do the work.\n"
        "- After completion, output a structured summary (see Step 5 in the protocol).\n"
        "- If you hit an escalation trigger, output ESCALATION: followed by the reason.\n"
        "- NEVER run git push. Deploy by merging locally on the VM and restarting supervisor.\n"
        "- All work happens locally on this VM. Working directory is {working_dir}.\n"
        "- YOU MUST output these exact strings when appropriate:\n"
        "  - VERIFIED FIXED (when you confirm the fix works end-to-end)\n"
        "  - ALL SMOKE TESTS PASS (when smoke tests pass)\n"
        "  - COMMIT: <sha> (after committing your changes)\n"
    ).format(
        mode=mode,
        understanding=understanding_output or "(No prior analysis available)",
        description=description,
        attachment_text=attachment_text,
        fix_context=fix_context,
        instruction=instruction,
        protocol=protocol,
        working_dir=effective_working_dir,
    )


def build_tester_prompt(description, commit_sha, files_changed, regression_checkpoints,
                        tenant=None):
    """Build prompt for the Regression Tester agent."""
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR
    return (
        "## ROLE: Regression Tester\n"
        "## MISSION: Try to BREAK the fix\n"
        "\n"
        "A fix was just deployed for this task:\n"
        "{description}\n"
        "\n"
        "Commit: {sha}\n"
        "Files changed: {files}\n"
        "\n"
        "## REGRESSION CHECK POINTS\n"
        "The analysis team identified these areas to test:\n"
        "{checkpoints}\n"
        "\n"
        "## YOUR INSTRUCTIONS\n"
        "1. Hit the fixed endpoint 10 times with varied inputs:\n"
        "   - Normal valid input\n"
        "   - Empty/blank input\n"
        "   - Special characters\n"
        "   - Different user roles (if applicable)\n"
        "   - Edge cases (very long strings, unicode, etc.)\n"
        "2. Hit each regression check point endpoint and verify it still works.\n"
        "3. Run the full smoke test:\n"
        "   python3 {validate_script} "
        "--base-url {test_base_url} "
        "--email {soak_email} --password {soak_password}\n"
        "4. Check gunicorn error log for new errors since the deploy:\n"
        "   tail -100 {error_log}\n"
        "\n"
        "Use python3 requests for all HTTP testing (never curl through SSH).\n"
        "Wait 20+ seconds after any supervisor restart before testing.\n"
        "\n"
        "## OUTPUT FORMAT\n"
        "End your response with exactly one of:\n"
        "VERDICT: PASS\n"
        "or\n"
        "VERDICT: FAIL\n"
        "followed by a summary of what passed, what failed, and specific error details.\n"
        "\n"
        "CRITICAL: Do NOT modify any files. Read-only analysis and testing only.\n"
        "\n"
        "TURN BUDGET: You have a limited number of tool-call turns. Be efficient:\n"
        "- Focus on the most important tests first.\n"
        "- Do not read files unnecessarily — test the live endpoints.\n"
        "- Reserve your LAST turn to write your VERDICT output.\n"
        "Working directory: {working_dir}\n"
    ).format(
        description=description,
        sha=commit_sha or "(unknown)",
        files=files_changed or "(unknown)",
        checkpoints=regression_checkpoints or "(none specified — test 5-10 related endpoints)",
        validate_script=VALIDATE_SCRIPT,
        test_base_url=config.TEST_BASE_URL,
        soak_email=SOAK_CHECK_EMAIL,
        soak_password=SOAK_CHECK_PASSWORD,
        error_log=config.ERROR_LOG_PATH,
        working_dir=effective_working_dir,
    )


def build_supabase_validator_prompt(description, commit_sha, files_changed,
                                    tenant=None):
    """Build prompt for the Supabase Validator agent."""
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR
    return (
        "## ROLE: Supabase Validator\n"
        "## MISSION: Verify database integrity after the fix\n"
        "\n"
        "A fix was just deployed for this task:\n"
        "{description}\n"
        "\n"
        "Commit: {sha}\n"
        "Files changed: {files}\n"
        "\n"
        "## YOUR INSTRUCTIONS\n"
        "1. Read the changed files to identify any modified Supabase queries.\n"
        "2. Verify those queries still return correct data by running test requests.\n"
        "3. Check that RLS policies weren't bypassed or broken:\n"
        "   - Query with one organization_id should NOT return data from another.\n"
        "4. Verify any new/modified queries have proper organization_id filters.\n"
        "5. Check for data leaks between tenants if the fix touched query logic.\n"
        "6. You can run read-only SQL queries via python3 to check schemas:\n"
        "   Use the Supabase client at {working_dir}/app/supabase_client.py\n"
        "\n"
        "## OUTPUT FORMAT\n"
        "End your response with exactly one of:\n"
        "VERDICT: PASS\n"
        "or\n"
        "VERDICT: FAIL\n"
        "followed by specific details of what you checked and any issues found.\n"
        "\n"
        "CRITICAL: Do NOT modify any files. Read-only analysis only.\n"
        "\n"
        "TURN BUDGET: You have a limited number of tool-call turns. Be efficient:\n"
        "- Read only the files directly related to the changed code.\n"
        "- Do not exhaustively explore the codebase.\n"
        "- Reserve your LAST turn to write your VERDICT output.\n"
        "Working directory: {working_dir}\n"
    ).format(
        description=description,
        sha=commit_sha or "(unknown)",
        files=files_changed or "(unknown)",
        working_dir=effective_working_dir,
    )


def _extract_assessor_context(impl_output):
    """Extract the most relevant parts of Implementer output for the Assessor.

    Pulls verification results, commit info, and summary sections rather than
    dumping raw 8000 chars of tool call noise.
    """
    if not impl_output:
        return "(no output)"

    # Look for structured summary sections (the protocol asks for these)
    sections = []

    # Extract everything after "VERIFIED FIXED" or "GATE 5:" to the end
    for marker in ["GATE 5:", "VERIFIED FIXED", "--- FIXER AGENT OUTPUT ---"]:
        idx = impl_output.rfind(marker)
        if idx != -1:
            sections.append(impl_output[idx:idx + 3000])

    # Extract commit lines
    for line in impl_output.split("\n"):
        stripped = line.strip()
        if stripped.startswith("COMMIT:") or "VERIFIED FIXED" in stripped:
            sections.append(stripped)

    # If we found structured content, use it; otherwise fall back to last 4000 chars
    if sections:
        combined = "\n\n".join(sections)
        # Still include the tail for context
        tail = impl_output[-2000:]
        return "{combined}\n\n--- Raw output tail ---\n{tail}".format(
            combined=combined[:4000], tail=tail,
        )
    else:
        return impl_output[-6000:]


def build_assessor_prompt(description, impl_output, tester_output,
                          validator_output, soak_result,
                          browser_smoke_result="", browser_tester_output="",
                          tenant=None):
    """Build prompt for the Final Assessor agent."""
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR
    assessor_context = _extract_assessor_context(impl_output)

    return (
        "## ROLE: Final Assessor\n"
        "## MISSION: Determine the definitive outcome of this task\n"
        "\n"
        "## TASK DESCRIPTION\n"
        "{description}\n"
        "\n"
        "## IMPLEMENTER OUTPUT (key sections)\n"
        "{impl_output}\n"
        "\n"
        "## REGRESSION TESTER OUTPUT\n"
        "{tester_output}\n"
        "\n"
        "## SUPABASE VALIDATOR OUTPUT\n"
        "{validator_output}\n"
        "\n"
        "## SOAK CHECK RESULT\n"
        "{soak_result}\n"
        "\n"
        "## BROWSER SMOKE TEST RESULT\n"
        "{browser_smoke_result}\n"
        "\n"
        "## BROWSER TESTER AGENT OUTPUT\n"
        "{browser_tester_output}\n"
        "\n"
        "## YOUR INSTRUCTIONS\n"
        "Review ALL the outputs above, including browser test results. You may also:\n"
        "- Hit live endpoints on {test_base_url} to verify the fix yourself\n"
        "- Read gunicorn logs: tail -50 {error_log}\n"
        "- Check the deployed code at {working_dir}/\n"
        "\n"
        "Then output your assessment.\n"
        "\n"
        "## OUTPUT FORMAT\n"
        "You MUST output exactly one of these verdict lines:\n"
        "\n"
        "VERDICT: FIXED\n"
        "(Bug is fixed, no regressions detected, all tests passed including browser tests)\n"
        "\n"
        "VERDICT: PARTIAL\n"
        "(Some aspects fixed but others still broken)\n"
        "\n"
        "VERDICT: FAILED\n"
        "(Fix did not resolve the issue)\n"
        "\n"
        "VERDICT: REGRESSION\n"
        "(Fix introduced new issues that weren't there before)\n"
        "\n"
        "VERDICT: ESCALATE\n"
        "(Needs human intervention — too complex, conflicting results, etc.)\n"
        "\n"
        "After the VERDICT line, provide:\n"
        "- EXPLANATION: [2-3 sentences explaining your verdict]\n"
        "- EVIDENCE: [specific test results or log entries that support your verdict]\n"
        "\n"
        "CRITICAL: Do NOT modify any files. Your ONLY job is to assess and report.\n"
        "\n"
        "TURN BUDGET: You have a limited number of tool-call turns. Be efficient:\n"
        "- You already have all the agent outputs above. Only use tools if you need\n"
        "  to verify something specific (e.g., hit the live endpoint once).\n"
        "- Do NOT spend turns reading source files — focus on the evidence provided.\n"
        "- Reserve your LAST turn to write your VERDICT output.\n"
        "Working directory: {working_dir}\n"
    ).format(
        description=description,
        impl_output=assessor_context,
        tester_output=tester_output or "(tester did not run)",
        validator_output=validator_output or "(validator did not run)",
        soak_result=soak_result or "(soak check did not run)",
        browser_smoke_result=browser_smoke_result or "(browser smoke test did not run)",
        browser_tester_output=browser_tester_output or "(browser tester did not run)",
        test_base_url=config.TEST_BASE_URL,
        error_log=config.ERROR_LOG_PATH,
        working_dir=effective_working_dir,
    )


def build_fixer_prompt(description, impl_output, tester_findings, validator_findings,
                       tenant=None):
    """Build prompt for the Fixer agent (runs only if regression detected)."""
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR

    # Read the protocol file
    protocol = ""
    try:
        with open(PROTOCOL_FILE, "r") as f:
            protocol = f.read()
    except Exception:
        protocol = "(Protocol file not available)"

    return (
        "## ROLE: Regression Fixer\n"
        "## MISSION: Fix the regression while preserving the original fix\n"
        "\n"
        "## ORIGINAL TASK\n"
        "{description}\n"
        "\n"
        "## WHAT THE IMPLEMENTER DID (last 5000 chars)\n"
        "{impl_output}\n"
        "\n"
        "## REGRESSION TESTER FINDINGS\n"
        "{tester_findings}\n"
        "\n"
        "## SUPABASE VALIDATOR FINDINGS\n"
        "{validator_findings}\n"
        "\n"
        "## YOUR INSTRUCTIONS\n"
        "1. Understand what the Implementer changed and why.\n"
        "2. Understand what broke (from Tester and Validator outputs).\n"
        "3. Fix the regression WITHOUT reverting the original fix.\n"
        "4. Test your fix end-to-end:\n"
        "   - The original bug should still be fixed\n"
        "   - The regression should be resolved\n"
        "5. Deploy: merge locally and restart supervisor.\n"
        "6. Run smoke test after deploy.\n"
        "\n"
        "## OPERATING PROTOCOL\n"
        "{protocol}\n"
        "\n"
        "## FINAL REMINDERS\n"
        "- You are autonomous. Do not ask for approval. Do the work.\n"
        "- NEVER run git push. Deploy by merging locally on the VM.\n"
        "- YOU MUST output these exact strings when appropriate:\n"
        "  - VERIFIED FIXED\n"
        "  - ALL SMOKE TESTS PASS\n"
        "  - COMMIT: <sha>\n"
        "- Working directory: {working_dir}\n"
    ).format(
        description=description,
        impl_output=impl_output[-5000:] if impl_output else "(no output)",
        tester_findings=tester_findings or "(none)",
        validator_findings=validator_findings or "(none)",
        protocol=protocol,
        working_dir=effective_working_dir,
    )


# ---------------------------------------------------------------------------
# Result Parsing
# ---------------------------------------------------------------------------


def parse_result(stdout):
    """Extract key info from agent output.

    Returns dict with: escalated, escalation_reason, validated, smoke_passed,
    rollback_happened, commit_sha, deployed.
    """
    escalated = bool(ESCALATION_PATTERN.search(stdout))
    escalation_reason = ""
    if escalated:
        for line in stdout.split("\n"):
            if "ESCALATION:" in line:
                escalation_reason = line.split("ESCALATION:", 1)[1].strip()
                break

    commit_sha = ""
    match = COMMIT_PATTERN.search(stdout)
    if match:
        commit_sha = match.group(1).strip()

    validated = bool(
        VERIFIED_FIXED_PATTERN.search(stdout)
        or SMOKE_PASS_PATTERN.search(stdout)
    )
    smoke_passed = bool(SMOKE_PASS_PATTERN.search(stdout))
    rollback_happened = bool(ROLLBACK_OK_PATTERN.search(stdout))

    # Check for deploy markers (merge completed and supervisor restarted)
    deployed = (
        ("merged" in stdout.lower() or "merge:" in stdout.lower())
        and "supervisorctl restart" in stdout.lower()
    )

    return {
        "escalated": escalated,
        "escalation_reason": escalation_reason,
        "validated": validated,
        "smoke_passed": smoke_passed,
        "rollback_happened": rollback_happened,
        "commit_sha": commit_sha,
        "deployed": deployed,
    }


# ---------------------------------------------------------------------------
# Pre-task Git Validation
# ---------------------------------------------------------------------------


def run_git_validate():
    """Run git guard validate before starting a task. Returns (ok, output)."""
    try:
        result = subprocess.run(
            ["python3", GIT_GUARD_SCRIPT, "validate"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=WORKING_DIR,
        )
        output = (result.stdout or "") + (result.stderr or "")
        ok = "FAIL:" not in output
        if not ok:
            log.warning("Git validation issue: %s", output[:300])
        return ok, output
    except Exception as e:
        log.error("Git validate failed: %s", e)
        return False, str(e)


def check_git_for_recent_commits(since_seconds_ago=3600):
    """Check if any commits were made in the last N seconds.

    Used after a timeout to detect if the agent committed code before being killed.
    Returns (has_commit, commit_sha, commit_message).
    """
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since={} seconds ago".format(since_seconds_ago),
             "--format=%H|%s", "-n", "1"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=WORKING_DIR,
        )
        output = result.stdout.strip()
        if output and "|" in output:
            sha, msg = output.split("|", 1)
            return True, sha.strip(), msg.strip()
        return False, "", ""
    except Exception as e:
        log.warning("Git commit check failed: %s", e)
        return False, "", ""


def check_git_dirty():
    """Check if working tree has uncommitted changes (agent was mid-work when killed).

    Returns (has_changes, files_changed_list).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=WORKING_DIR,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        # Exclude ai_ops_worker.py itself (always shows as modified)
        files = [f for f in files if "ai_ops_worker" not in f]
        return bool(files), files
    except Exception:
        return False, []


# ---------------------------------------------------------------------------
# Post-deploy Soak Check
# ---------------------------------------------------------------------------


def run_soak_check():
    """Run the post-deploy soak check. Returns (passed, output)."""
    try:
        result = subprocess.run(
            [
                "python3", VALIDATE_SCRIPT,
                "--base-url", config.TEST_BASE_URL,
                "--email", SOAK_CHECK_EMAIL,
                "--password", SOAK_CHECK_PASSWORD,
                "--soak",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=WORKING_DIR,
        )
        output = (result.stdout or "") + (result.stderr or "")
        passed = "SOAK TEST PASS" in output
        log.info("Soak check: %s", "PASS" if passed else "FAIL")
        return passed, output
    except subprocess.TimeoutExpired:
        log.error("Soak check timed out")
        return False, "Soak check timed out after 120s"
    except Exception as e:
        log.error("Soak check error: %s", e)
        return False, str(e)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def run_rollback():
    """Run git guard rollback. Returns (ok, output)."""
    try:
        result = subprocess.run(
            ["python3", GIT_GUARD_SCRIPT, "rollback"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=WORKING_DIR,
        )
        output = (result.stdout or "") + (result.stderr or "")
        log.info("Rollback output: %s", output[:300])
        return result.returncode == 0, output
    except Exception as e:
        log.error("Rollback failed: %s", e)
        return False, str(e)


# ---------------------------------------------------------------------------
# Browser Smoke Test (deterministic, Playwright)
# ---------------------------------------------------------------------------


def run_browser_smoke_test(base_url=None):
    """Run deterministic browser smoke tests. Returns (passed, output)."""
    base_url = base_url or config.TEST_BASE_URL
    try:
        result = subprocess.run(
            [
                config.PYTHON_PATH,
                BROWSER_SMOKE_SCRIPT,
                "--base-url", base_url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=WORKING_DIR,
            env={**os.environ, "HOME": config.VM_HOME},
        )
        output = (result.stdout or "") + (result.stderr or "")
        passed = "BROWSER SMOKE TEST PASS" in output
        log.info("Browser smoke test (%s): %s", base_url, "PASS" if passed else "FAIL")
        return passed, output
    except subprocess.TimeoutExpired:
        log.error("Browser smoke test timed out")
        return False, "Browser smoke test timed out after 120s"
    except Exception as e:
        log.error("Browser smoke test error: %s", e)
        return False, str(e)


# ---------------------------------------------------------------------------
# Smart Soak Period
# ---------------------------------------------------------------------------


def classify_soak_sensitivity(files_changed: str) -> tuple[int, str]:
    """Determine soak duration based on files touched.

    Returns (soak_seconds, reason).
    """
    if not files_changed:
        return SOAK_NORMAL_SECONDS, "normal (no files detected)"

    for pattern in SENSITIVE_FILE_PATTERNS:
        if pattern in files_changed.lower():
            return SOAK_SENSITIVE_SECONDS, "sensitive (touches {p})".format(p=pattern)

    return SOAK_NORMAL_SECONDS, "normal"


def run_smart_soak(svc, session_id, soak_seconds: int, reason: str) -> tuple[bool, str]:
    """Run smart soak period with error log monitoring.

    Monitors gunicorn error log every SOAK_MONITOR_INTERVAL seconds.
    Re-runs browser smoke test at the end.
    Returns (passed, details).
    """
    log.info("Starting smart soak: %ds (%s)", soak_seconds, reason)
    svc.add_message(
        session_id, "system", "Worker",
        "Smart soak started: {s}s ({r}). Monitoring for errors...".format(
            s=soak_seconds, r=reason,
        ),
        message_type="status_update",
    )

    error_log = config.ERROR_LOG_PATH
    # Snapshot current error log size
    try:
        initial_size = os.path.getsize(error_log)
    except OSError:
        initial_size = 0

    soak_start = time.monotonic()
    new_errors = []

    while time.monotonic() - soak_start < soak_seconds:
        time.sleep(SOAK_MONITOR_INTERVAL)

        # Check for new errors in log
        try:
            current_size = os.path.getsize(error_log)
            if current_size > initial_size:
                with open(error_log, "r") as f:
                    f.seek(initial_size)
                    new_content = f.read()
                # Look for actual errors (not just access log noise)
                for line in new_content.split("\n"):
                    line_lower = line.lower()
                    if any(kw in line_lower for kw in ["error", "traceback", "exception", "critical"]):
                        if not any(skip in line_lower for skip in ["healthcheck", "favicon", "robots.txt"]):
                            new_errors.append(line.strip()[:200])
                initial_size = current_size
        except Exception as e:
            log.debug("Error checking log during soak: %s", e)

        if new_errors:
            detail = "; ".join(new_errors[:3])
            log.warning("Soak detected new errors: %s", detail)
            return False, "Soak FAILED: new errors during soak: {d}".format(d=detail)

    # Re-run browser smoke test at end of soak
    log.info("Soak period complete, running final browser smoke test...")
    browser_ok, browser_output = run_browser_smoke_test()
    if not browser_ok:
        return False, "Soak FAILED: post-soak browser test failed: {o}".format(
            o=browser_output[:300],
        )

    elapsed = int(time.monotonic() - soak_start)
    return True, "Soak PASSED: {s}s clean, browser test OK".format(s=elapsed)


# ---------------------------------------------------------------------------
# Production Deploy
# ---------------------------------------------------------------------------


def _deploy_to_production(svc, session_id, commit_sha: str) -> tuple[bool, str]:
    """Deploy to production VM.

    1. Push commits to GitHub (already done by Implementer)
    2. SSH to production VM and run deploy script
    3. Run browser smoke test against production
    Returns (success, details).
    """
    log.info("Deploying to production: commit %s", commit_sha[:12])
    svc.add_message(
        session_id, "system", "Worker",
        f"Deploying to production ({PRODUCTION_BASE_URL})...",
        message_type="status_update",
    )

    # Step 1: Ensure commits are pushed to GitHub
    try:
        push_result = subprocess.run(
            ["git", "push", "origin", "main"],
            capture_output=True, text=True, timeout=60,
            cwd=WORKING_DIR,
        )
        if push_result.returncode != 0:
            log.warning("Git push stderr: %s", push_result.stderr[:300])
    except Exception as e:
        log.warning("Git push failed (may already be pushed): %s", e)

    # Step 2: SSH to production VM and run deploy script
    try:
        deploy_cmd = [
            "gcloud", "compute", "ssh", PRODUCTION_VM,
            "--zone", PRODUCTION_VM_ZONE,
            "--command", f"sudo -u {config.VM_USER} bash {PRODUCTION_DEPLOY_SCRIPT} {commit_sha}",
        ]
        deploy_result = subprocess.run(
            deploy_cmd,
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "HOME": config.VM_HOME},
        )
        deploy_output = (deploy_result.stdout or "") + (deploy_result.stderr or "")

        if deploy_result.returncode != 0:
            log.error("Production deploy failed (rc=%d): %s", deploy_result.returncode, deploy_output[:500])
            return False, f"Production deploy failed: {deploy_output[:300]}"

        if "DEPLOYMENT SUCCESSFUL" not in deploy_output:
            log.warning("Production deploy output missing success marker")
            return False, f"Production deploy uncertain: {deploy_output[:300]}"

        log.info("Production deploy script succeeded")
    except subprocess.TimeoutExpired:
        log.error("Production deploy timed out after 300s")
        return False, "Production deploy timed out after 300s"
    except Exception as e:
        log.error("Production deploy error: %s", e)
        return False, f"Production deploy error: {str(e)[:200]}"

    # Step 3: Run browser smoke test against production
    svc.add_message(
        session_id, "system", "Worker",
        "Production deploy done. Running browser smoke test against production...",
        message_type="status_update",
    )
    prod_browser_ok, prod_browser_output = run_browser_smoke_test(PRODUCTION_BASE_URL)

    if not prod_browser_ok:
        log.error("Production browser smoke test failed: %s", prod_browser_output[:300])
        # Rollback on production
        try:
            prod_app_dir = config.PRODUCTION_APP_DIR
            rollback_cmd = [
                "gcloud", "compute", "ssh", PRODUCTION_VM,
                "--zone", PRODUCTION_VM_ZONE,
                "--command",
                f"cd {prod_app_dir} && "
                "PREV=$(ls -1td releases/*/ 2>/dev/null | sed -n 2p | tr -d /) && "
                "if [ -n \"$PREV\" ]; then "
                f"  sudo -u {config.VM_USER} ln -sfn {prod_app_dir}/releases/$PREV {prod_app_dir}/current && "
                f"  sudo supervisorctl restart {config.PRODUCTION_SUPERVISOR_NAME}; "
                "fi",
            ]
            subprocess.run(rollback_cmd, capture_output=True, text=True, timeout=60)
            log.info("Production rollback attempted")
        except Exception as e:
            log.error("Production rollback failed: %s", e)

        return False, f"Production browser test failed (rolled back): {prod_browser_output[:200]}"

    return True, "Production deploy + browser test PASSED"


# ---------------------------------------------------------------------------
# Agent Invocation with Streaming
# ---------------------------------------------------------------------------


def run_agent_streaming(svc, session_id, queue_id, prompt,
                        max_turns=None, timeout=None, working_dir=None):
    """Invoke Claude Code agent with line-by-line output streaming.

    Streams gate progress to Supabase messages so the web UI can poll.
    Uses select() for non-blocking reads and a separate thread for stderr
    to prevent pipe deadlocks.

    Returns dict with: success, stdout, stderr, returncode, elapsed_seconds, timed_out.
    """
    max_turns = max_turns or IMPLEMENTER_MAX_TURNS
    timeout = timeout or AGENT_TIMEOUT
    effective_cwd = working_dir or WORKING_DIR

    cmd = [
        "claude", "--print",
        "--model", AGENT_MODEL,
        "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep,Task",
        "--max-turns", str(max_turns),
        "-p", prompt,
    ]

    env = os.environ.copy()
    env["CI"] = "true"
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["HOME"] = config.VM_HOME

    log.info(
        "Starting agent (session_id=%s, queue_id=%s, timeout=%ds, max_turns=%d)",
        session_id, queue_id, timeout, max_turns,
    )

    start_time = time.monotonic()
    stdout_lines = []
    stderr_lines = []
    gates_reached = set()

    def _stderr_reader(pipe):
        """Read stderr in a separate thread to prevent pipe buffer deadlock."""
        try:
            for line in pipe:
                stderr_lines.append(line)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=effective_cwd,
            env=env,
        )

        # Start stderr reader thread to prevent deadlock
        stderr_thread = threading.Thread(
            target=_stderr_reader, args=(proc.stderr,), daemon=True,
        )
        stderr_thread.start()

        # Read stdout with select() for non-blocking timeout checks
        while True:
            # Check for timeout
            elapsed = time.monotonic() - start_time
            if elapsed > timeout:
                log.error("Agent timed out after %ds, killing process", int(elapsed))
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                stderr_thread.join(timeout=5)
                return {
                    "success": False,
                    "stdout": "\n".join(stdout_lines),
                    "stderr": "Agent timed out after {}s".format(timeout),
                    "returncode": -1,
                    "elapsed_seconds": int(elapsed),
                    "timed_out": True,
                }

            # Check for shutdown signal
            if shutdown:
                log.warning("Shutdown requested, terminating agent process")
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                stderr_thread.join(timeout=5)
                return {
                    "success": False,
                    "stdout": "\n".join(stdout_lines),
                    "stderr": "Worker shutdown requested",
                    "returncode": -2,
                    "elapsed_seconds": int(time.monotonic() - start_time),
                    "timed_out": False,
                }

            # Non-blocking read with 5-second poll via select()
            ready, _, _ = select.select([proc.stdout], [], [], 5.0)
            if not ready:
                # No output for 5 seconds — loop back to check timeout/shutdown
                continue

            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if not line:
                continue

            line = line.rstrip("\n")
            stdout_lines.append(line)

            # --- Gate detection ---
            for pattern, gate_key, gate_label in GATE_PATTERNS:
                if pattern.search(line) and gate_key not in gates_reached:
                    gates_reached.add(gate_key)
                    log.info("Agent reached %s", gate_label)
                    try:
                        svc.add_message(
                            session_id, "system", "Agent",
                            "Reached {label}".format(label=gate_label),
                            message_type="status_update",
                        )
                        svc.update_session(session_id, gate_reached=gate_key)
                    except Exception as e:
                        log.warning("Failed to stream gate progress: %s", e)

            # --- Escalation detection ---
            if ESCALATION_PATTERN.search(line):
                log.warning("Agent escalated: %s", line[:200])
                try:
                    svc.add_message(
                        session_id, "system", "Agent",
                        "ESCALATION: {reason}".format(
                            reason=line.split("ESCALATION:", 1)[1].strip()[:500]
                            if "ESCALATION:" in line else "Unknown reason"
                        ),
                        message_type="error",
                    )
                except Exception as e:
                    log.warning("Failed to stream escalation: %s", e)

            # --- Verified fixed / smoke tests (deduplicated) ---
            if VERIFIED_FIXED_PATTERN.search(line) and "verified_fixed" not in gates_reached:
                gates_reached.add("verified_fixed")
                try:
                    svc.add_message(
                        session_id, "system", "Agent",
                        "Fix verified - endpoint confirmed working",
                        message_type="status_update",
                    )
                except Exception:
                    pass

            if SMOKE_PASS_PATTERN.search(line) and "smoke_passed" not in gates_reached:
                gates_reached.add("smoke_passed")
                try:
                    svc.add_message(
                        session_id, "system", "Agent",
                        "All smoke tests passed",
                        message_type="status_update",
                    )
                except Exception:
                    pass

            if ROLLBACK_OK_PATTERN.search(line) and "rollback_ok" not in gates_reached:
                gates_reached.add("rollback_ok")
                try:
                    svc.add_message(
                        session_id, "system", "Agent",
                        "Rollback completed successfully",
                        message_type="status_update",
                    )
                except Exception:
                    pass

        # Wait for stderr thread to finish collecting
        stderr_thread.join(timeout=5)
        stderr_text = "".join(stderr_lines)

        elapsed = int(time.monotonic() - start_time)
        returncode = proc.returncode

        return {
            "success": returncode == 0,
            "stdout": "\n".join(stdout_lines),
            "stderr": stderr_text[-2000:],
            "returncode": returncode,
            "elapsed_seconds": elapsed,
            "timed_out": False,
        }

    except Exception as e:
        elapsed = int(time.monotonic() - start_time)
        log.error("Agent process error: %s", e)
        return {
            "success": False,
            "stdout": "\n".join(stdout_lines),
            "stderr": str(e),
            "returncode": -1,
            "elapsed_seconds": elapsed,
            "timed_out": False,
        }


# ---------------------------------------------------------------------------
# Multi-Agent Helpers
# ---------------------------------------------------------------------------


def run_agent_single(prompt, model=None, max_turns=25, timeout=180,
                     allowed_tools="Bash,Read,Glob,Grep", working_dir=None):
    """Run a single non-streaming Claude agent and capture stdout.

    Used for shorter agents (specialists, tester, validator, assessor).
    Retries once on transient failures (rate limits, network errors).
    Returns dict with: success, stdout, stderr, elapsed_seconds, timed_out.
    """
    model = model or AGENT_MODEL
    effective_cwd = working_dir or WORKING_DIR
    cmd = [
        "claude", "--print",
        "--model", model,
        "--allowedTools", allowed_tools,
        "--max-turns", str(max_turns),
        "-p", prompt,
    ]

    env = os.environ.copy()
    env["CI"] = "true"
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["HOME"] = config.VM_HOME

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        start_time = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=effective_cwd,
                env=env,
            )
            elapsed = int(time.monotonic() - start_time)

            # Retry on non-zero exit with rate limit or network error
            stderr_lower = (result.stderr or "").lower()
            if (
                result.returncode != 0
                and attempt < max_attempts
                and ("rate limit" in stderr_lower or "network" in stderr_lower
                     or "connection" in stderr_lower or "overloaded" in stderr_lower)
            ):
                log.warning(
                    "Agent transient failure (attempt %d/%d, rc=%d): %s. Retrying in 30s...",
                    attempt, max_attempts, result.returncode, stderr_lower[:200],
                )
                time.sleep(30)
                continue

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout or "",
                "stderr": (result.stderr or "")[-2000:],
                "elapsed_seconds": elapsed,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            elapsed = int(time.monotonic() - start_time)
            log.warning("Agent timed out after %ds (limit=%ds)", elapsed, timeout)
            return {
                "success": False,
                "stdout": "",
                "stderr": "Agent timed out after {}s".format(timeout),
                "elapsed_seconds": elapsed,
                "timed_out": True,
            }
        except Exception as e:
            elapsed = int(time.monotonic() - start_time)
            if attempt < max_attempts:
                log.warning(
                    "Agent error (attempt %d/%d): %s. Retrying in 30s...",
                    attempt, max_attempts, e,
                )
                time.sleep(30)
                continue
            log.error("Agent process error: %s", e)
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "elapsed_seconds": elapsed,
                "timed_out": False,
            }

    # Should not reach here, but just in case
    return {
        "success": False,
        "stdout": "",
        "stderr": "All retry attempts exhausted",
        "elapsed_seconds": 0,
        "timed_out": False,
    }


def run_parallel_agents(agent_configs, working_dir=None):
    """Run 2-4 agents concurrently via Popen, collect all outputs.

    Args:
        agent_configs: list of dicts, each with:
            - name: str (e.g., "route_tracer")
            - prompt: str
            - model: str (default AGENT_MODEL)
            - max_turns: int
            - timeout: int (seconds)
            - allowed_tools: str
        working_dir: Optional override for the working directory.

    Returns: dict mapping name -> result dict (same shape as run_agent_single).
    """
    effective_cwd = working_dir or WORKING_DIR
    env = os.environ.copy()
    env["CI"] = "true"
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["HOME"] = config.VM_HOME

    processes = {}
    start_times = {}

    for cfg in agent_configs:
        name = cfg["name"]
        model = cfg.get("model", AGENT_MODEL)
        cmd = [
            "claude", "--print",
            "--model", model,
            "--allowedTools", cfg.get("allowed_tools", "Bash,Read,Glob,Grep"),
            "--max-turns", str(cfg.get("max_turns", SPECIALIST_MAX_TURNS)),
            "-p", cfg["prompt"],
        ]

        log.info("Starting parallel agent: %s", name)
        start_times[name] = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=effective_cwd,
                env=env,
            )
            processes[name] = proc
        except Exception as e:
            log.error("Failed to start agent %s: %s", name, e)
            processes[name] = None

    # Wait for all — use remaining wall time from each agent's start
    results = {}

    for cfg in agent_configs:
        name = cfg["name"]
        timeout = cfg.get("timeout", SPECIALIST_TIMEOUT)
        proc = processes.get(name)

        if proc is None:
            results[name] = {
                "success": False,
                "stdout": "",
                "stderr": "Failed to start process",
                "elapsed_seconds": 0,
                "timed_out": False,
            }
            continue

        # Calculate remaining time for this agent (accounts for parallel execution)
        elapsed_so_far = time.monotonic() - start_times[name]
        remaining = max(timeout - elapsed_so_far, 5)  # at least 5s to collect output

        try:
            stdout, stderr = proc.communicate(timeout=remaining)
            elapsed = int(time.monotonic() - start_times[name])
            results[name] = {
                "success": proc.returncode == 0,
                "stdout": stdout or "",
                "stderr": (stderr or "")[-2000:],
                "elapsed_seconds": elapsed,
                "timed_out": False,
            }
            log.info(
                "Parallel agent %s finished: rc=%s, elapsed=%ds",
                name, proc.returncode, elapsed,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            # Read any buffered output that was produced before the timeout
            partial_stdout = ""
            partial_stderr = ""
            try:
                partial_stdout, partial_stderr = proc.communicate(timeout=15)
            except Exception:
                pass
            elapsed = int(time.monotonic() - start_times[name])
            has_output = bool(partial_stdout and partial_stdout.strip())
            log.warning(
                "Parallel agent %s timed out after %ds (captured %d chars of partial output)",
                name, elapsed, len(partial_stdout or ""),
            )
            results[name] = {
                "success": has_output,  # Partial output is still useful
                "stdout": partial_stdout or "",
                "stderr": (partial_stderr or "")[-2000:],
                "elapsed_seconds": elapsed,
                "timed_out": True,
            }
        except Exception as e:
            elapsed = int(time.monotonic() - start_times[name])
            log.error("Parallel agent %s error: %s", name, e)
            results[name] = {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "elapsed_seconds": elapsed,
                "timed_out": False,
            }

    return results


def is_agent_output_valid(stdout):
    """Check if agent output is meaningful (not an error message or empty).

    Returns (is_valid, reason) tuple.
    """
    if not stdout or not stdout.strip():
        return False, "empty output"
    if MAX_TURNS_ERROR_PATTERN.search(stdout):
        return False, "reached max turns"
    if len(stdout.strip()) < 50:
        return False, "output too short ({} chars)".format(len(stdout.strip()))
    return True, "ok"


def extract_agent_verdict(output):
    """Extract VERDICT: PASS or VERDICT: FAIL from agent output.

    Returns "PASS", "FAIL", or "NONE" if no verdict found.
    """
    if not output:
        return "NONE"
    upper = output.upper()
    if "VERDICT: PASS" in upper:
        return "PASS"
    if "VERDICT: FAIL" in upper:
        return "FAIL"
    return "NONE"


_VERDICT_PATTERN = re.compile(
    r"^\s*(?:\*\*|#+\s*)?VERDICT:\s*(\w+)", re.MULTILINE,
)
_EXPLANATION_PATTERN = re.compile(
    r"^\s*(?:\*\*|#+\s*)?EXPLANATION:\s*(.+)", re.MULTILINE,
)


def extract_final_verdict(assessor_stdout):
    """Parse the Assessor's VERDICT line from stdout.

    Uses flexible regex to handle markdown formatting (##, **, etc.).
    Returns dict with: verdict (str), explanation (str).
    """
    verdict = ""
    explanation = ""

    # Find all verdict matches and take the last one (assessor may quote others)
    verdict_matches = list(_VERDICT_PATTERN.finditer(assessor_stdout))
    if verdict_matches:
        raw_verdict = verdict_matches[-1].group(1).strip().upper()
        valid_verdicts = {"FIXED", "PARTIAL", "FAILED", "REGRESSION", "ESCALATE"}
        if raw_verdict in valid_verdicts:
            verdict = raw_verdict
        else:
            log.warning(
                "Unrecognized assessor verdict: '%s', defaulting to needs_review",
                raw_verdict,
            )

    explanation_matches = list(_EXPLANATION_PATTERN.finditer(assessor_stdout))
    if explanation_matches:
        explanation = explanation_matches[-1].group(1).strip()

    return {"verdict": verdict, "explanation": explanation}


def extract_regression_checkpoints(consolidator_output):
    """Extract regression check points from Consolidator output."""
    lines = consolidator_output.split("\n")
    in_section = False
    checkpoints = []

    for line in lines:
        stripped = line.strip()
        if "Regression Check Points" in line or "regression check points" in line.lower():
            in_section = True
            continue
        if in_section:
            # Stop at the next section header
            if stripped.startswith("###") or stripped.startswith("## "):
                break
            if stripped.startswith("- ") or stripped.startswith("* "):
                checkpoints.append(stripped[2:])
            elif stripped and not stripped.startswith("#"):
                checkpoints.append(stripped)

    return "\n".join(checkpoints) if checkpoints else ""


def extract_files_changed(impl_stdout):
    """Extract files changed from Implementer output (look for COMMIT or git diff)."""
    files = []
    for line in impl_stdout.split("\n"):
        stripped = line.strip()
        # Look for common git diff output patterns
        if stripped.startswith("modified:") or stripped.startswith("new file:"):
            fname = stripped.split(":", 1)[1].strip()
            if fname:
                files.append(fname)
        elif stripped.startswith("M ") or stripped.startswith("A "):
            fname = stripped[2:].strip()
            if fname:
                files.append(fname)
    return ", ".join(files) if files else ""


def verdict_to_status(verdict_dict):
    """Map assessor verdict to session status.

    Returns: (status, regression_detected)
    """
    verdict = verdict_dict.get("verdict", "")
    mapping = {
        "FIXED": ("completed", False),
        "PARTIAL": ("needs_review", False),
        "FAILED": ("failed", False),
        "REGRESSION": ("failed", True),
        "ESCALATE": ("escalated", False),
    }
    result = mapping.get(verdict)
    if result is None:
        log.warning(
            "Unrecognized verdict '%s' in verdict_to_status, defaulting to needs_review",
            verdict,
        )
        return ("needs_review", False)
    return result


# ---------------------------------------------------------------------------
# Understanding Phase — Multi-Agent (4 parallel specialists + 1 consolidator)
# ---------------------------------------------------------------------------


def process_understanding(svc, item, tenant=None):
    """Run multi-agent understanding phase: 4 parallel specialists + 1 consolidator."""
    queue_id = item["id"]
    session_id = item["session_id"]
    task_type = item.get("task_type", "bug")
    description = item.get("description", "")
    attachments = item.get("attachments") or []
    task_label = "Bug Fix" if task_type == "bug" else "New Feature"

    log.info(
        "Understanding phase (multi-agent): queue_id=%s, session_id=%s, type=%s",
        queue_id, session_id, task_label,
    )

    # Clean up old system/agent messages from previous runs (prevents duplicate content)
    try:
        sb = svc.supabase
        sb.table("ai_ops_messages").delete().eq(
            "session_id", session_id
        ).neq("sender_type", "user").execute()
        log.info("Cleaned up old messages for session %s before re-run", session_id[:8])
    except Exception as e:
        log.warning("Failed to clean old messages: %s", e)

    # Re-add the bug report description so it's visible in the session panel
    if description and description.strip():
        try:
            svc.add_message(
                session_id, "system", "Bug Report",
                description[:5000],
                message_type="bug_report",
            )
        except Exception as e:
            log.warning("Failed to re-add bug report message: %s", e)

    # Mark as running
    try:
        svc.update_queue_item(
            queue_id,
            status="running",
            picked_up_at=datetime.now(timezone.utc).isoformat(),
        )
        svc.update_session(session_id, status="running")
        svc.add_message(
            session_id, "system", "Worker",
            "5 specialist agents are analyzing your request in parallel "
            "(Route Tracer, Service Analyst, Security Analyst, Frontend Inspector, Supabase Specialist)...",
            message_type="status_update",
        )
    except Exception as e:
        log.error("Failed to mark understanding task as running: %s", e)
        return

    # Notify
    send_sms(
        f'{config.APP_NAME} AI Ops: Analyzing {task_label.lower()} with 6-agent team -- '
        f'"{description[:70]}". Will post understanding for your review.'
    )

    phase_start = time.monotonic()
    agent_team_log = []
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR

    # Sync tenant workspace before running agents
    if tenant:
        try:
            sync_workspace(tenant)
            log.info("Workspace synced for tenant %s", tenant.slug)
        except Exception as e:
            log.error("Workspace sync failed for tenant %s: %s", tenant.slug, e)
            svc.add_message(session_id, "system", "Worker",
                            f"Workspace sync failed: {e}",
                            message_type="error")

    # === Phase 1: Run 5 specialists in batches (3+2) ===
    all_roles = ["route_tracer", "service_analyst", "security_analyst", "frontend_inspector", "supabase_specialist"]
    batches = []
    for i in range(0, len(all_roles), SPECIALIST_PARALLEL_BATCH):
        batches.append(all_roles[i:i + SPECIALIST_PARALLEL_BATCH])

    specialist_results = {}
    specialist_outputs = {}

    for batch_num, batch_roles in enumerate(batches, 1):
        batch_configs = []
        for role in batch_roles:
            batch_configs.append({
                "name": role,
                "prompt": build_specialist_prompt(role, task_type, description, attachments,
                                                  tenant=tenant),
                "model": AGENT_MODEL,
                "max_turns": SPECIALIST_MAX_TURNS,
                "timeout": SPECIALIST_TIMEOUT,
                "allowed_tools": "Bash,Read,Glob,Grep",
            })

        names = ", ".join(SPECIALIST_ROLES[r]["title"] for r in batch_roles)
        log.info("Starting specialist batch %d/%d: %s", batch_num, len(batches), names)
        svc.add_message(
            session_id, "system", "Worker",
            "Running specialist batch {b}/{t}: {names}...".format(
                b=batch_num, t=len(batches), names=names,
            ),
            message_type="status_update",
        )

        batch_results = run_parallel_agents(batch_configs, working_dir=effective_working_dir)
        specialist_results.update(batch_results)

    # Collect outputs from all batches
    for role in all_roles:
        result = specialist_results.get(role, {})
        title = SPECIALIST_ROLES[role]["title"]
        stdout = result.get("stdout", "").strip()
        elapsed = result.get("elapsed_seconds", 0)
        timed_out = result.get("timed_out", False)

        agent_team_log.append({
            "agent": title,
            "phase": "understanding",
            "elapsed_seconds": elapsed,
            "timed_out": timed_out,
            "success": bool(stdout),
            "output_length": len(stdout),
        })

        if stdout:
            specialist_outputs[title] = stdout
            log.info("Specialist %s: %d chars in %ds", title, len(stdout), elapsed)
        else:
            log.warning("Specialist %s: no output (timed_out=%s)", title, timed_out)

    successful_specialists = len(specialist_outputs)
    log.info(
        "Specialist phase complete: %d/%d produced output",
        successful_specialists, len(SPECIALIST_ROLES),
    )

    svc.add_message(
        session_id, "system", "Worker",
        f"{successful_specialists}/{len(all_roles)} specialists completed. Running consolidator...",
        message_type="status_update",
    )

    # === Phase 2: Run consolidator ===
    understanding = ""
    if specialist_outputs:
        consolidator_prompt = build_consolidator_prompt(
            task_type, description, specialist_outputs,
            tenant=tenant,
        )

        log.info("Starting consolidator agent...")
        consolidator_result = run_agent_single(
            consolidator_prompt,
            model=AGENT_MODEL,
            max_turns=CONSOLIDATOR_MAX_TURNS,
            timeout=CONSOLIDATOR_TIMEOUT,
            allowed_tools="Bash,Read,Glob,Grep",
            working_dir=effective_working_dir,
        )

        understanding = consolidator_result.get("stdout", "").strip()
        agent_team_log.append({
            "agent": "Consolidator",
            "phase": "understanding",
            "elapsed_seconds": consolidator_result.get("elapsed_seconds", 0),
            "timed_out": consolidator_result.get("timed_out", False),
            "success": bool(understanding),
            "output_length": len(understanding),
        })

        if not understanding:
            log.warning("Consolidator produced no output, using best specialist output")
            # Fallback: use longest specialist output
            best_output = max(specialist_outputs.values(), key=len)
            understanding = best_output

    total_elapsed = int(time.monotonic() - phase_start)
    session_url = "{base}/ai-ops/session/{sid}".format(
        base=APP_BASE_URL, sid=session_id,
    )

    if understanding.strip():
        # Parse dual output: technical analysis + user summary
        technical = understanding.strip()
        user_summary = ""

        tech_match = re.search(
            r"===TECHNICAL_ANALYSIS_START===\s*(.*?)\s*===TECHNICAL_ANALYSIS_END===",
            understanding, re.DOTALL,
        )
        user_match = re.search(
            r"===USER_SUMMARY_START===\s*(.*?)\s*===USER_SUMMARY_END===",
            understanding, re.DOTALL,
        )

        if tech_match:
            technical = tech_match.group(1).strip()
        if user_match:
            user_summary = user_match.group(1).strip()

        # Post technical analysis as agent message (admin-only via plan type)
        svc.add_message(
            session_id, "agent", "AI Agent",
            technical,
            message_type="plan",
        )

        # Post user-facing summary as separate message (visible to all)
        if user_summary:
            svc.add_message(
                session_id, "agent", "AI Agent",
                user_summary,
                message_type="user_summary",
            )

        # Store understanding and team log on session
        session_update = {
            "status": "awaiting_approval",
            "understanding_output": technical,
            "agent_team_log": json.dumps(agent_team_log),
        }
        if user_summary:
            session_update["user_summary"] = user_summary
        svc.update_session(session_id, **session_update)
        svc.update_queue_item(
            queue_id,
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            result_summary=(
                f"Multi-agent understanding: {successful_specialists}/{len(all_roles)} specialists, "
                f"consolidator OK, {total_elapsed}s total"
            ),
        )

        send_sms(
            f'{config.APP_NAME} AI Ops: Analysis ready for "{description[:50]}" '
            f'({successful_specialists} specialists, {total_elapsed}s). Review and approve: {session_url}'
        )
        log.info(
            "Understanding posted (multi-agent), awaiting approval (elapsed=%ds)",
            total_elapsed,
        )

        # --- Auto-approve check ---
        # Auto-approve if: (a) auto-detected bug, (b) user set auto_approve toggle
        # Feature requests NEVER auto-approve.
        try:
            sess_data = svc.get_session(session_id)
            sess_title = (sess_data or {}).get("title", "")
            sess_mode = (sess_data or {}).get("mode", "")
            is_feature = sess_mode == "new_feature" or sess_title.startswith("Feature Request:")
            should_auto = (
                sess_title.startswith("Auto Bug:")
                or (sess_data or {}).get("auto_approve")
            )

            if should_auto and not is_feature:
                log.info("Auto-approving session %s (title=%s, auto_approve=%s)",
                         session_id, sess_title[:40], (sess_data or {}).get("auto_approve"))
                svc.add_message(
                    session_id, "system", "Bug Intake",
                    "Auto-approved. Starting execution.",
                    message_type="status_update",
                )
                svc.queue_task(
                    session_id, task_type, description, attachments,
                    phase="execute", understanding_output=technical,
                )
                svc.update_session(session_id, status="queued")
                send_sms(
                    f"{config.APP_NAME}: Auto-approved. Starting fix for: {description[:80]}"
                )
        except Exception as e:
            log.error("Auto-approve check failed: %s", e)

    else:
        # All specialists and consolidator failed — queue direct execution
        log.warning(
            "Multi-agent understanding produced no output, "
            "falling back to direct execution"
        )
        svc.add_message(
            session_id, "system", "Worker",
            "Analysis phase could not produce an understanding (all specialists failed). "
            "Proceeding directly to execution.",
            message_type="status_update",
        )
        svc.update_queue_item(
            queue_id,
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            result_summary="Multi-agent understanding failed, queuing direct execution",
        )
        svc.queue_task(
            session_id, task_type, description, attachments, phase="execute",
        )
        svc.update_session(session_id, status="queued")


# ---------------------------------------------------------------------------
# Execution Phase — Multi-Agent Pipeline
# ---------------------------------------------------------------------------


def process_execution_multi(svc, item, understanding_output=None, tenant=None):
    """Run multi-agent execution: Implementer → Tester → Validator → Assessor (+ Fixer)."""
    queue_id = item["id"]
    session_id = item["session_id"]
    task_type = item.get("task_type", "bug")
    description = item.get("description", "")
    attachments = item.get("attachments") or []
    task_label = "Bug Fix" if task_type == "bug" else "New Feature"
    effective_working_dir = tenant.workspace_path if tenant else WORKING_DIR

    log.info(
        "Execution phase (multi-agent): queue_id=%s, session_id=%s, type=%s",
        queue_id, session_id, task_label,
    )

    # --- Mark as running ---
    try:
        svc.update_queue_item(
            queue_id,
            status="running",
            picked_up_at=datetime.now(timezone.utc).isoformat(),
        )
        svc.update_session(session_id, status="running")
        svc.add_message(
            session_id, "system", "Worker",
            "Multi-agent execution starting: Implementer agent is working...",
            message_type="status_update",
        )
    except Exception as e:
        log.error("Failed to mark task as running: %s", e)
        return

    # --- Sync tenant workspace ---
    if tenant:
        try:
            sync_workspace(tenant)
            log.info("Workspace synced for tenant %s", tenant.slug)
        except Exception as e:
            log.error("Workspace sync failed for tenant %s: %s", tenant.slug, e)
            svc.add_message(session_id, "system", "Worker",
                            f"Workspace sync failed: {e}",
                            message_type="error")

    # --- Start usage record ---
    usage_record_id = None
    tenant_id = item.get("tenant_id")
    if tenant_id:
        try:
            record_type = "feature" if task_type == "feature" else "bug_fix"
            usage_record_id = start_usage_record(tenant_id, session_id, record_type)
            log.info("Usage record started: %s", usage_record_id)
        except Exception as e:
            log.warning("Failed to start usage record: %s", e)

    # --- Pre-task git validation ---
    git_ok, git_output = run_git_validate()
    if not git_ok:
        svc.add_message(
            session_id, "system", "Worker",
            "Warning: Git state was dirty from a previous run. Auto-cleaned: {output}".format(
                output=git_output[:300],
            ),
            message_type="status_update",
        )
        log.warning("Git state auto-cleaned before task: %s", git_output[:300])

    session_url = "{base}/ai-ops/session/{sid}".format(
        base=APP_BASE_URL, sid=session_id,
    )
    send_sms(
        f'{config.APP_NAME} AI Ops: Approved. Multi-agent team working on {task_label.lower()}. '
        f'"{description[:50]}" ~15-30 min. {session_url}'
    )

    phase_start = time.monotonic()
    agent_team_log = []

    # Try to load existing team log from understanding phase
    try:
        sess = svc.get_session(session_id)
        if sess and sess.get("agent_team_log"):
            existing_log = sess["agent_team_log"]
            if isinstance(existing_log, str):
                agent_team_log = json.loads(existing_log)
            elif isinstance(existing_log, list):
                agent_team_log = existing_log
    except Exception:
        pass

    # =========================================================================
    # Agent 1: Implementer (streaming, full access) — with auto-retry on timeout
    # =========================================================================

    def _run_implementer(attempt=1, timeout_override=None):
        """Run the Implementer agent. Returns (impl_result, impl_parsed, impl_stdout)."""
        timeout_val = timeout_override or IMPLEMENTER_TIMEOUT
        log.info("Starting Implementer agent (attempt %d, timeout=%ds)...", attempt, timeout_val)
        svc.add_message(
            session_id, "system", "Worker",
            "Agent 1/4: Implementer is working on the fix{retry}...".format(
                retry=" (retry)" if attempt > 1 else "",
            ),
            message_type="status_update",
        )

        # Build prompt — use enhanced prompt with understanding if available
        if understanding_output:
            prompt = build_implementer_prompt(
                task_type, description, attachments, understanding_output,
                tenant=tenant,
            )
        else:
            prompt = build_prompt(task_type, description, attachments, tenant=tenant)

        result = run_agent_streaming(
            svc, session_id, queue_id, prompt,
            max_turns=IMPLEMENTER_MAX_TURNS, timeout=timeout_val,
            working_dir=effective_working_dir,
        )
        stdout = result.get("stdout", "")
        parsed = parse_result(stdout)

        agent_team_log.append({
            "agent": "Implementer" if attempt == 1 else "Implementer (retry)",
            "phase": "execution",
            "elapsed_seconds": result.get("elapsed_seconds", 0),
            "timed_out": result.get("timed_out", False),
            "success": parsed.get("validated", False),
            "commit_sha": parsed.get("commit_sha", ""),
            "deployed": parsed.get("deployed", False),
        })

        log.info(
            "Implementer done (attempt %d): elapsed=%ds, validated=%s, deployed=%s, commit=%s",
            attempt,
            result.get("elapsed_seconds", 0),
            parsed.get("validated"),
            parsed.get("deployed"),
            parsed.get("commit_sha", "")[:12],
        )

        return result, parsed, stdout

    impl_result, impl_parsed, impl_stdout = _run_implementer(attempt=1)

    # --- P0: Check for timeout FIRST (before output validity) ---
    # When the agent times out, stdout is typically empty (claude --print buffers output).
    # Check git to see if the agent committed code before being killed.
    if impl_result.get("timed_out"):
        elapsed_s = impl_result.get("elapsed_seconds", 0)
        log.warning("Implementer timed out after %ds — checking git for commits...", elapsed_s)

        # Check if the agent committed code before the timeout
        has_commit, git_sha, git_msg = check_git_for_recent_commits(elapsed_s + 60)
        has_dirty, dirty_files = check_git_dirty()

        if has_commit:
            log.info("Found commit %s after timeout: %s", git_sha[:12], git_msg[:100])
            impl_parsed["commit_sha"] = git_sha
            impl_stdout = "Agent timed out but committed: {} — {}".format(git_sha[:12], git_msg)
            svc.add_message(
                session_id, "system", "Worker",
                "Implementer timed out but committed code ({sha}). "
                "Proceeding with downstream validation.".format(sha=git_sha[:12]),
                message_type="status_update",
            )
        elif has_dirty:
            log.info("Agent timed out with uncommitted changes in: %s",
                     ", ".join(dirty_files[:5]))
            svc.add_message(
                session_id, "system", "Worker",
                "Implementer timed out with uncommitted changes in {n} files. "
                "Retrying with extended timeout...".format(n=len(dirty_files)),
                message_type="status_update",
            )
            # Clean up partial changes before retry
            try:
                subprocess.run(
                    ["git", "checkout", "."],
                    capture_output=True, text=True, timeout=10,
                    cwd=WORKING_DIR,
                )
            except Exception:
                pass

            # Auto-retry with 50% more time
            retry_timeout = int(IMPLEMENTER_TIMEOUT * 1.5)
            impl_result, impl_parsed, impl_stdout = _run_implementer(
                attempt=2, timeout_override=retry_timeout,
            )
            # If retry also timed out, fall through to failure below
        else:
            # No commit, no dirty files — agent produced nothing. Retry once.
            log.warning("No commits or changes after timeout — retrying once")
            svc.add_message(
                session_id, "system", "Worker",
                "Implementer timed out with no output. Retrying with extended timeout...",
                message_type="status_update",
            )
            retry_timeout = int(IMPLEMENTER_TIMEOUT * 1.5)
            impl_result, impl_parsed, impl_stdout = _run_implementer(
                attempt=2, timeout_override=retry_timeout,
            )

    # If still timed out after retry, check git one more time then fail
    if impl_result.get("timed_out"):
        elapsed_s = impl_result.get("elapsed_seconds", 0)
        has_commit, git_sha, git_msg = check_git_for_recent_commits(elapsed_s + 60)
        if has_commit:
            log.info("Found commit %s after retry timeout: %s", git_sha[:12], git_msg[:100])
            impl_parsed["commit_sha"] = git_sha
            impl_stdout = "Agent timed out but committed: {} — {}".format(git_sha[:12], git_msg)
        else:
            log.error("Implementer timed out on both attempts — no commits found")
            svc.add_message(
                session_id, "system", "Worker",
                "Implementer timed out after {s}s (both attempts). No code committed. "
                "This task may require manual intervention or decomposition.".format(s=elapsed_s),
                message_type="error",
            )
            _finalize_execution(
                svc, session_id, queue_id, description, task_label,
                "failed",
                "Implementer timed out after {s}s (2 attempts, no commits)".format(s=elapsed_s),
                impl_parsed, impl_result, agent_team_log, phase_start,
            )
            return

    # --- Check for escalation ---
    if impl_parsed.get("escalated"):
        log.warning("Implementer escalated — skipping downstream agents")
        _finalize_execution(
            svc, session_id, queue_id, description, task_label,
            "escalated",
            "ESCALATED: {reason}".format(
                reason=impl_parsed.get("escalation_reason", "Unknown")
            ),
            impl_parsed, impl_result, agent_team_log, phase_start,
        )
        return

    # --- Check output validity ---
    # If the Implementer committed code, treat output as valid even if short
    impl_valid, impl_valid_reason = is_agent_output_valid(impl_stdout)
    if not impl_valid and impl_parsed.get("commit_sha"):
        log.info("Implementer output short but has commit %s — treating as valid",
                 impl_parsed["commit_sha"][:12])
        impl_valid = True
        impl_valid_reason = "ok (short output but committed)"

    # Retry once on empty/short output (agent may have failed silently)
    if not impl_valid and not impl_result.get("timed_out"):
        log.warning(
            "Implementer output invalid (%s) without timeout — checking git then retrying once",
            impl_valid_reason,
        )
        # Check if there's a commit we missed
        has_commit, git_sha, git_msg = check_git_for_recent_commits(
            impl_result.get("elapsed_seconds", 0) + 60,
        )
        if has_commit:
            log.info("Found commit %s despite empty output: %s", git_sha[:12], git_msg[:100])
            impl_parsed["commit_sha"] = git_sha
            impl_stdout = "Agent output was empty but committed: {} — {}".format(git_sha[:12], git_msg)
            impl_valid = True
            impl_valid_reason = "ok (empty output but committed)"
        else:
            # Retry the Implementer once
            svc.add_message(
                session_id, "system", "Worker",
                "Implementer produced no usable output ({reason}). Retrying...".format(
                    reason=impl_valid_reason,
                ),
                message_type="status_update",
            )
            impl_result, impl_parsed, impl_stdout = _run_implementer(attempt=2)
            impl_valid, impl_valid_reason = is_agent_output_valid(impl_stdout)
            if not impl_valid and impl_parsed.get("commit_sha"):
                log.info("Retry: output short but has commit %s", impl_parsed["commit_sha"][:12])
                impl_valid = True
                impl_valid_reason = "ok (short output but committed on retry)"

    if not impl_valid:
        log.error("Implementer output invalid: %s", impl_valid_reason)
        svc.add_message(
            session_id, "system", "Worker",
            "Implementer agent output was invalid ({reason}). "
            "Skipping downstream agents.".format(reason=impl_valid_reason),
            message_type="error",
        )
        _finalize_execution(
            svc, session_id, queue_id, description, task_label,
            "failed",
            "Implementer output invalid: {reason}".format(reason=impl_valid_reason),
            impl_parsed, impl_result, agent_team_log, phase_start,
        )
        return

    # Extract info for downstream agents
    commit_sha = impl_parsed.get("commit_sha", "")
    files_changed = extract_files_changed(impl_stdout)
    regression_checkpoints = ""
    if understanding_output:
        regression_checkpoints = extract_regression_checkpoints(understanding_output)

    # --- Helper: check total pipeline wall-clock timeout ---
    def _check_pipeline_timeout(agent_name):
        """Return True if we've exceeded MULTI_AGENT_TOTAL_TIMEOUT."""
        elapsed = time.monotonic() - phase_start
        if elapsed > MULTI_AGENT_TOTAL_TIMEOUT:
            log.error(
                "Total pipeline timeout exceeded before %s (%ds > %ds)",
                agent_name, int(elapsed), MULTI_AGENT_TOTAL_TIMEOUT,
            )
            svc.add_message(
                session_id, "system", "Worker",
                "Pipeline timeout exceeded ({elapsed}s). "
                "Stopping before {agent}.".format(
                    elapsed=int(elapsed), agent=agent_name,
                ),
                message_type="error",
            )
            _finalize_execution(
                svc, session_id, queue_id, description, task_label,
                "failed",
                "Pipeline wall-clock timeout ({elapsed}s > {limit}s) before {agent}".format(
                    elapsed=int(elapsed), limit=MULTI_AGENT_TOTAL_TIMEOUT,
                    agent=agent_name,
                ),
                impl_parsed, impl_result, agent_team_log, phase_start,
            )
            return True
        return False

    # =========================================================================
    # Post-deploy soak check (runs between Implementer and Tester)
    # =========================================================================
    soak_passed = False
    soak_output = ""

    if impl_parsed.get("deployed") and impl_parsed.get("validated"):
        log.info("Running post-deploy soak check...")
        svc.add_message(
            session_id, "system", "Worker",
            "Running post-deploy soak check...",
            message_type="status_update",
        )
        soak_passed, soak_output = run_soak_check()

        if not soak_passed:
            log.warning("Soak check failed, initiating rollback...")
            svc.add_message(
                session_id, "system", "Worker",
                "Soak check FAILED. Initiating rollback...",
                message_type="status_update",
            )
            rollback_ok, rollback_output = run_rollback()
            _finalize_execution(
                svc, session_id, queue_id, description, task_label,
                "failed",
                "Soak check failed. Rollback {status}.".format(
                    status="OK" if rollback_ok else "FAILED",
                ),
                impl_parsed, impl_result, agent_team_log, phase_start,
                rollback_happened=True,
            )
            return

    # =========================================================================
    # Agents 2+3: Regression Tester + Supabase Validator (PARALLEL, read-only)
    # =========================================================================
    if _check_pipeline_timeout("Tester+Validator"):
        return

    tester_output = ""
    tester_valid = False
    tester_verdict = "NONE"
    tester_reason = "not run"
    validator_output = ""
    validator_valid = False
    validator_verdict = "NONE"
    validator_reason = "not run"

    log.info("Starting Regression Tester + Supabase Validator in parallel...")
    svc.add_message(
        session_id, "system", "Worker",
        "Agents 2-3/4: Regression Tester + Supabase Validator running in parallel...",
        message_type="status_update",
    )

    tester_prompt = build_tester_prompt(
        description, commit_sha, files_changed, regression_checkpoints,
        tenant=tenant,
    )
    validator_prompt = build_supabase_validator_prompt(
        description, commit_sha, files_changed,
        tenant=tenant,
    )

    parallel_results = run_parallel_agents([
        {
            "name": "regression_tester",
            "prompt": tester_prompt,
            "model": AGENT_MODEL,
            "max_turns": TESTER_MAX_TURNS,
            "timeout": TESTER_TIMEOUT,
            "allowed_tools": "Bash,Read,Glob,Grep",
        },
        {
            "name": "supabase_validator",
            "prompt": validator_prompt,
            "model": AGENT_MODEL,
            "max_turns": SUPABASE_VALIDATOR_MAX_TURNS,
            "timeout": SUPABASE_VALIDATOR_TIMEOUT,
            "allowed_tools": "Bash,Read,Glob,Grep",
        },
    ], working_dir=effective_working_dir)

    # Process Tester results
    tester_result = parallel_results.get("regression_tester", {})
    tester_output = tester_result.get("stdout", "")
    tester_valid, tester_reason = is_agent_output_valid(tester_output)
    tester_verdict = extract_agent_verdict(tester_output)

    agent_team_log.append({
        "agent": "Regression Tester",
        "phase": "execution",
        "elapsed_seconds": tester_result.get("elapsed_seconds", 0),
        "timed_out": tester_result.get("timed_out", False),
        "output_length": len(tester_output),
        "output_valid": tester_valid,
        "output_reason": tester_reason,
        "verdict": tester_verdict,
    })

    log.info(
        "Regression Tester done: %d chars, %ds, valid=%s (%s), verdict=%s",
        len(tester_output), tester_result.get("elapsed_seconds", 0),
        tester_valid, tester_reason, tester_verdict,
    )

    # Process Validator results
    validator_result = parallel_results.get("supabase_validator", {})
    validator_output = validator_result.get("stdout", "")
    validator_valid, validator_reason = is_agent_output_valid(validator_output)
    validator_verdict = extract_agent_verdict(validator_output)

    agent_team_log.append({
        "agent": "Supabase Validator",
        "phase": "execution",
        "elapsed_seconds": validator_result.get("elapsed_seconds", 0),
        "timed_out": validator_result.get("timed_out", False),
        "output_length": len(validator_output),
        "output_valid": validator_valid,
        "output_reason": validator_reason,
        "verdict": validator_verdict,
    })

    log.info(
        "Supabase Validator done: %d chars, %ds, valid=%s (%s), verdict=%s",
        len(validator_output), validator_result.get("elapsed_seconds", 0),
        validator_valid, validator_reason, validator_verdict,
    )

    # =========================================================================
    # Check for regressions — run Fixer if needed
    # =========================================================================
    tester_failed = tester_verdict == "FAIL"
    validator_failed = validator_verdict == "FAIL"
    needs_fixer = tester_failed or validator_failed
    retry_count = 0

    if needs_fixer and retry_count < MAX_FIX_RETRY_CYCLES:
        if _check_pipeline_timeout("Fixer"):
            return

        retry_count += 1
        log.warning(
            "Regression detected (tester=%s, validator=%s), running Fixer (attempt %d)...",
            "FAIL" if tester_failed else "PASS",
            "FAIL" if validator_failed else "PASS",
            retry_count,
        )
        svc.add_message(
            session_id, "system", "Worker",
            "Regression detected! Running Fixer agent to resolve...",
            message_type="status_update",
        )

        # --- Run Fixer ---
        fixer_prompt = build_fixer_prompt(
            description, impl_stdout,
            tester_output if tester_failed else "",
            validator_output if validator_failed else "",
        )
        fixer_result = run_agent_streaming(
            svc, session_id, queue_id, fixer_prompt,
            max_turns=FIXER_MAX_TURNS, timeout=FIXER_TIMEOUT,
        )
        fixer_stdout = fixer_result.get("stdout", "")
        fixer_parsed = parse_result(fixer_stdout)

        # Validate Fixer output (same max-turns check as Implementer)
        fixer_valid, fixer_reason = is_agent_output_valid(fixer_stdout)
        if not fixer_valid:
            log.warning("Fixer output invalid: %s", fixer_reason)
            svc.add_message(
                session_id, "system", "Worker",
                "Fixer agent output was invalid ({reason}). "
                "Proceeding to assessment.".format(reason=fixer_reason),
                message_type="status_update",
            )

        agent_team_log.append({
            "agent": "Fixer",
            "phase": "execution",
            "elapsed_seconds": fixer_result.get("elapsed_seconds", 0),
            "timed_out": fixer_result.get("timed_out", False),
            "success": fixer_parsed.get("validated", False),
            "commit_sha": fixer_parsed.get("commit_sha", ""),
            "output_valid": fixer_valid,
        })

        # Update commit SHA if fixer committed
        if fixer_parsed.get("commit_sha"):
            commit_sha = fixer_parsed["commit_sha"]

        # Merge fixer output into impl output for assessor
        impl_stdout = impl_stdout + "\n\n--- FIXER AGENT OUTPUT ---\n" + fixer_stdout
        impl_parsed = parse_result(impl_stdout)

        # Re-run soak check after fixer deploy
        if fixer_parsed.get("deployed"):
            soak_passed, soak_output = run_soak_check()
            if not soak_passed:
                rollback_ok, _ = run_rollback()
                _finalize_execution(
                    svc, session_id, queue_id, description, task_label,
                    "failed",
                    "Fixer deploy failed soak check. Rollback {status}.".format(
                        status="OK" if rollback_ok else "FAILED",
                    ),
                    impl_parsed, impl_result, agent_team_log, phase_start,
                    rollback_happened=True, retry_count=retry_count,
                )
                return

        # Re-run Tester + Validator in parallel after fixer
        if not _check_pipeline_timeout("Tester+Validator retry"):
            log.info("Re-running Tester + Validator in parallel after Fixer...")
            svc.add_message(
                session_id, "system", "Worker",
                "Re-running Tester + Validator in parallel after fix...",
                message_type="status_update",
            )

            retry_results = run_parallel_agents([
                {
                    "name": "regression_tester_retry",
                    "prompt": build_tester_prompt(
                        description, commit_sha, files_changed, regression_checkpoints,
                    ),
                    "model": AGENT_MODEL,
                    "max_turns": TESTER_MAX_TURNS,
                    "timeout": TESTER_TIMEOUT,
                    "allowed_tools": "Bash,Read,Glob,Grep",
                },
                {
                    "name": "supabase_validator_retry",
                    "prompt": build_supabase_validator_prompt(
                        description, commit_sha, files_changed,
                    ),
                    "model": AGENT_MODEL,
                    "max_turns": SUPABASE_VALIDATOR_MAX_TURNS,
                    "timeout": SUPABASE_VALIDATOR_TIMEOUT,
                    "allowed_tools": "Bash,Read,Glob,Grep",
                },
            ])

            tester_output = retry_results.get("regression_tester_retry", {}).get("stdout", "")
            agent_team_log.append({
                "agent": "Regression Tester (retry)",
                "phase": "execution",
                "elapsed_seconds": retry_results.get("regression_tester_retry", {}).get("elapsed_seconds", 0),
            })

            validator_output = retry_results.get("supabase_validator_retry", {}).get("stdout", "")
            agent_team_log.append({
                "agent": "Supabase Validator (retry)",
                "phase": "execution",
                "elapsed_seconds": retry_results.get("supabase_validator_retry", {}).get("elapsed_seconds", 0),
            })

    # =========================================================================
    # Browser Smoke Test (deterministic, Playwright Python script)
    # =========================================================================
    browser_smoke_result = ""
    browser_smoke_passed = False

    if _check_pipeline_timeout("Browser Smoke Test"):
        return

    log.info("Running deterministic browser smoke test...")
    svc.add_message(
        session_id, "system", "Worker",
        "Running browser smoke test (Playwright)...",
        message_type="status_update",
    )

    browser_smoke_passed, browser_smoke_result = run_browser_smoke_test()

    agent_team_log.append({
        "agent": "Browser Smoke Test",
        "phase": "execution",
        "passed": browser_smoke_passed,
        "output": browser_smoke_result[:500],
    })

    log.info("Browser smoke test: %s", "PASS" if browser_smoke_passed else "FAIL")

    if not browser_smoke_passed:
        log.warning("Browser smoke test FAILED — initiating rollback")
        svc.add_message(
            session_id, "system", "Worker",
            "Browser smoke test FAILED: {r}. Rolling back...".format(
                r=browser_smoke_result[:200],
            ),
            message_type="status_update",
        )
        rollback_ok, _ = run_rollback()
        _finalize_execution(
            svc, session_id, queue_id, description, task_label,
            "failed",
            "Browser smoke test failed. Rollback {s}.".format(
                s="OK" if rollback_ok else "FAILED",
            ),
            impl_parsed, impl_result, agent_team_log, phase_start,
            rollback_happened=True,
        )
        return

    # =========================================================================
    # Browser Tester Agent (exploratory, claude --print with Playwright MCP)
    # =========================================================================
    browser_tester_output = ""

    if _check_pipeline_timeout("Browser Tester Agent"):
        return

    log.info("Starting Browser Tester agent (exploratory, with Playwright MCP)...")
    svc.add_message(
        session_id, "system", "Worker",
        "Browser Tester agent verifying fix in real browser...",
        message_type="status_update",
    )

    browser_tester_prompt = (
        "## ROLE: Browser Tester\n"
        "## MISSION: Verify the bug fix works in a real browser\n"
        "\n"
        "## BUG DESCRIPTION\n"
        "{description}\n"
        "\n"
        "## WHAT WAS FIXED\n"
        "{impl_summary}\n"
        "\n"
        "## FILES CHANGED\n"
        "{files_changed}\n"
        "\n"
        "## YOUR INSTRUCTIONS\n"
        "You have Playwright MCP tools. Use them to:\n"
        "1. Navigate to the page(s) affected by this bug fix\n"
        "2. Interact with the page like a real user — click buttons, fill forms, scroll\n"
        "3. Verify the fix actually works visually\n"
        "4. Check for broken buttons, forms, or layouts on the affected page(s)\n"
        "5. Take a screenshot if something looks wrong\n"
        "\n"
        "Base URL: {test_base_url}\n"
        "Login: {soak_email} / {soak_password}\n"
        "\n"
        "## OUTPUT FORMAT\n"
        "VERDICT: PASS or VERDICT: FAIL\n"
        "EXPLANATION: [what you tested and found]\n"
        "\n"
        "Be efficient — focus on the specific fix, not a full site audit.\n"
    ).format(
        description=description,
        impl_summary=impl_stdout[-3000:] if impl_stdout else "(no output)",
        files_changed=files_changed or "(unknown)",
        test_base_url=config.TEST_BASE_URL,
        soak_email=SOAK_CHECK_EMAIL,
        soak_password=SOAK_CHECK_PASSWORD,
    )

    browser_tester_result = run_agent_single(
        browser_tester_prompt,
        model=AGENT_MODEL,
        max_turns=BROWSER_TESTER_MAX_TURNS,
        timeout=BROWSER_TESTER_TIMEOUT,
        allowed_tools="Bash,Read,Glob,Grep,mcp__playwright__browser_navigate,mcp__playwright__browser_click,mcp__playwright__browser_type,mcp__playwright__browser_screenshot,mcp__playwright__browser_wait,mcp__playwright__browser_select_option,mcp__playwright__browser_hover,mcp__playwright__browser_press_key,mcp__playwright__browser_handle_dialog,mcp__playwright__browser_tab_new,mcp__playwright__browser_tab_select,mcp__playwright__browser_tab_close",
    )
    browser_tester_output = browser_tester_result.get("stdout", "")
    browser_tester_verdict = extract_agent_verdict(browser_tester_output)

    agent_team_log.append({
        "agent": "Browser Tester",
        "phase": "execution",
        "elapsed_seconds": browser_tester_result.get("elapsed_seconds", 0),
        "timed_out": browser_tester_result.get("timed_out", False),
        "output_length": len(browser_tester_output),
        "verdict": browser_tester_verdict,
    })

    log.info(
        "Browser Tester done: %d chars, %ds, verdict=%s",
        len(browser_tester_output),
        browser_tester_result.get("elapsed_seconds", 0),
        browser_tester_verdict,
    )

    # =========================================================================
    # Final Assessor
    # =========================================================================
    if _check_pipeline_timeout("Assessor"):
        return

    log.info("Starting Final Assessor agent...")
    svc.add_message(
        session_id, "system", "Worker",
        "Final Assessor determining outcome...",
        message_type="status_update",
    )

    soak_text = ""
    if soak_output:
        soak_text = "PASS" if soak_passed else "FAIL\n" + soak_output[:1000]
    else:
        soak_text = "Not run (agent did not deploy or validate)"

    # Annotate tester/validator output for the Assessor if they had issues
    tester_for_assessor = tester_output
    if not tester_valid:
        tester_for_assessor = (
            "WARNING: Tester agent output was invalid ({reason}). "
            "You MUST test the fix yourself to compensate.\n\n"
            "Raw output: {output}"
        ).format(reason=tester_reason, output=tester_output or "(empty)")

    validator_for_assessor = validator_output
    if not validator_valid:
        validator_for_assessor = (
            "WARNING: Validator agent output was invalid ({reason}). "
            "You MUST check the database queries yourself to compensate.\n\n"
            "Raw output: {output}"
        ).format(reason=validator_reason, output=validator_output or "(empty)")

    assessor_prompt = build_assessor_prompt(
        description, impl_stdout, tester_for_assessor, validator_for_assessor, soak_text,
        browser_smoke_result=browser_smoke_result,
        browser_tester_output=browser_tester_output,
    )
    assessor_result = run_agent_single(
        assessor_prompt,
        model=AGENT_MODEL,
        max_turns=ASSESSOR_MAX_TURNS,
        timeout=ASSESSOR_TIMEOUT,
    )
    assessor_stdout = assessor_result.get("stdout", "")

    agent_team_log.append({
        "agent": "Final Assessor",
        "phase": "execution",
        "elapsed_seconds": assessor_result.get("elapsed_seconds", 0),
        "timed_out": assessor_result.get("timed_out", False),
        "output_length": len(assessor_stdout),
    })

    log.info(
        "Assessor done: %d chars, %ds",
        len(assessor_stdout), assessor_result.get("elapsed_seconds", 0),
    )

    # =========================================================================
    # Determine final status from Assessor verdict
    # =========================================================================
    verdict_dict = extract_final_verdict(assessor_stdout)
    verdict = verdict_dict.get("verdict", "")
    explanation = verdict_dict.get("explanation", "")

    if verdict:
        final_status, regression_detected = verdict_to_status(verdict_dict)
        log.info(
            "Assessor verdict: %s → status=%s, regression=%s",
            verdict, final_status, regression_detected,
        )

        # Auto-rollback on REGRESSION verdict
        if verdict == "REGRESSION" and impl_parsed.get("deployed"):
            log.warning("REGRESSION verdict — initiating rollback")
            svc.add_message(
                session_id, "system", "Worker",
                "Assessor found regression. Rolling back...",
                message_type="status_update",
            )
            rollback_ok, rollback_output = run_rollback()
            svc.add_message(
                session_id, "system", "Worker",
                "Rollback {status}.".format(
                    status="OK" if rollback_ok else "FAILED",
                ),
                message_type="status_update",
            )
    else:
        # No verdict from assessor — fall back to marker-based parsing
        log.warning("Assessor produced no verdict, falling back to marker-based status")
        regression_detected = False
        if impl_parsed.get("escalated"):
            final_status = "escalated"
        elif impl_parsed.get("validated") and (impl_parsed.get("smoke_passed") or soak_passed):
            final_status = "completed"
        elif impl_parsed.get("validated"):
            final_status = "completed"
        else:
            final_status = "needs_review"

    # =========================================================================
    # Smart Soak Period + Auto-Deploy to Production (only on FIXED verdict)
    # =========================================================================
    prod_deployed = False
    prod_deploy_detail = ""

    if verdict == "FIXED" and commit_sha:
        # Classify soak sensitivity
        soak_duration, soak_reason = classify_soak_sensitivity(files_changed)
        log.info("Smart soak: %ds (%s)", soak_duration, soak_reason)

        smart_soak_ok, smart_soak_detail = run_smart_soak(
            svc, session_id, soak_duration, soak_reason,
        )

        agent_team_log.append({
            "agent": "Smart Soak",
            "phase": "execution",
            "duration_seconds": soak_duration,
            "reason": soak_reason,
            "passed": smart_soak_ok,
            "detail": smart_soak_detail[:300],
        })

        if not smart_soak_ok:
            log.warning("Smart soak failed: %s", smart_soak_detail[:200])
            svc.add_message(
                session_id, "system", "Worker",
                f"Smart soak FAILED: {smart_soak_detail[:200]}. Skipping production deploy.",
                message_type="status_update",
            )
            final_status = "needs_review"
        else:
            # Soak passed — deploy to production
            log.info("Smart soak passed. Deploying to production...")
            prod_ok, prod_detail = _deploy_to_production(svc, session_id, commit_sha)

            agent_team_log.append({
                "agent": "Production Deploy",
                "phase": "execution",
                "passed": prod_ok,
                "detail": prod_detail[:300],
            })

            if prod_ok:
                prod_deployed = True
                prod_deploy_detail = prod_detail
                final_status = "deployed_production"
                svc.add_message(
                    session_id, "system", "Worker",
                    "Auto-deployed to production. All checks passed.",
                    message_type="status_update",
                )
                send_sms(
                    f'{config.APP_NAME} AI Ops: Bug auto-deployed to production '
                    f'({PRODUCTION_BASE_URL}). "{description[:50]}" Commit: {commit_sha[:12]}'
                )
            else:
                prod_deploy_detail = prod_detail
                svc.add_message(
                    session_id, "system", "Worker",
                    f"Production deploy failed: {prod_detail[:200]}. Test server still has the fix.",
                    message_type="status_update",
                )
                # Don't change final_status — the fix is still good on test

    # Build summary
    total_elapsed = int(time.monotonic() - phase_start)
    elapsed_min = total_elapsed // 60
    elapsed_sec = total_elapsed % 60

    summary_parts = []
    if verdict:
        summary_parts.append(f"Assessor verdict: {verdict}")
    if explanation:
        summary_parts.append(explanation[:200])
    if commit_sha:
        summary_parts.append(f"Commit: {commit_sha}")
    if soak_passed:
        summary_parts.append("Soak check passed")
    if prod_deployed:
        summary_parts.append("Deployed to production")
    if prod_deploy_detail and not prod_deployed:
        summary_parts.append(f"Production deploy failed: {prod_deploy_detail[:100]}")
    if retry_count > 0:
        summary_parts.append("Fixer ran {n} time(s)".format(n=retry_count))

    summary = ". ".join(summary_parts) if summary_parts else "Agent finished."
    summary += " Elapsed: {m}m{s}s.".format(m=elapsed_min, s=elapsed_sec)

    _finalize_execution(
        svc, session_id, queue_id, description, task_label,
        final_status, summary,
        impl_parsed, impl_result, agent_team_log, phase_start,
        soak_passed=soak_passed, soak_output=soak_output,
        verdict=verdict, explanation=explanation,
        regression_detected=regression_detected, retry_count=retry_count,
    )


def _finalize_execution(svc, session_id, queue_id, description, task_label,
                         final_status, summary, impl_parsed, impl_result,
                         agent_team_log, phase_start,
                         soak_passed=False, soak_output="",
                         rollback_happened=False, verdict="", explanation="",
                         regression_detected=False, retry_count=0):
    """Finalize execution: update DB, log fix memory, send notifications."""
    total_elapsed = int(time.monotonic() - phase_start)
    elapsed_min = total_elapsed // 60
    elapsed_sec = total_elapsed % 60

    # --- Update session and queue ---
    try:
        svc.update_session(
            session_id,
            status=final_status,
            summary=summary,
            commit_sha=impl_parsed.get("commit_sha", ""),
            agent_elapsed_seconds=total_elapsed,
            assessor_verdict=verdict or None,
            assessor_explanation=explanation or None,
            agent_team_log=json.dumps(agent_team_log),
            regression_detected=regression_detected,
            retry_count=retry_count,
        )
        svc.update_queue_item(
            queue_id,
            status="completed" if final_status in ("completed", "needs_review") else "failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            result_summary=summary[:1000],
        )
        svc.add_message(
            session_id, "system", "Worker",
            "Task {status}: {summary}".format(status=final_status, summary=summary),
            message_type="status_update",
        )
    except Exception as e:
        log.error("Failed to update session/queue with results: %s", e)

    # --- Fix memory logging (on success) ---
    if final_status == "completed":
        try:
            from fix_memory import append_fix
            append_fix(queue_id, impl_result.get("stdout", ""))
        except ImportError:
            log.debug("fix_memory module not available -- skipping fix logging")
        except Exception as e:
            log.debug("Fix memory logging failed: %s", e)


    # --- In-app notification for users when bug is auto-fixed ---
    if verdict == "FIXED" and final_status == "completed":
        try:
            from app.services.notification_service import NotificationService
            # Get organization_id from session or use default
            sess_data = svc.get_session(session_id)
            org_id = (sess_data or {}).get("organization_id")
            if not org_id:
                # Get org from the first user in ai_ops_users
                org_result = svc.supabase.table("users").select("organization_id").limit(1).execute()
                org_id = (org_result.data[0]["organization_id"]) if org_result.data else None
            if org_id:
                title_text = (sess_data or {}).get("title", "Bug fix")
                ns = NotificationService(org_id)
                ns.notify_organization_admins(
                    notification_type="bug_fixed",
                    title="Bug Auto-Fixed",
                    message="AI Ops automatically fixed: %s" % title_text[:100],
                    related_entity_type="ai_ops_session",
                    related_entity_id=session_id,
                    priority="normal",
                )
                log.info("Created bug_fixed notification for session %s", session_id[:8])
        except Exception as e:
            log.warning("Failed to create bug_fixed notification: %s", e)

    # --- Notifications ---
    _send_notifications(
        impl_parsed, impl_result, task_label, description, summary,
        soak_passed, soak_output, elapsed_min, elapsed_sec,
        session_id, verdict=verdict,
    )

    # --- Bug Intake: update linked bug report status ---
    _update_bug_status_from_verdict(svc, session_id, verdict or final_status)

    log.info(
        "Execution complete: queue_id=%s, status=%s, verdict=%s, elapsed=%dm%ds",
        queue_id, final_status, verdict or "(none)", elapsed_min, elapsed_sec,
    )


# ---------------------------------------------------------------------------
# Process a Single Task
# ---------------------------------------------------------------------------


def process_task(svc, item):
    """Process a single queue item — dispatch to understanding or execution phase."""
    session_id = item["session_id"]
    queue_id = item["id"]
    phase = item.get("phase", "execute")

    # --- Load tenant context ---
    tenant_id = item.get("tenant_id")
    tenant = None
    if tenant_id:
        try:
            tenant = load_tenant(tenant_id)
        except Exception as e:
            log.error("Failed to load tenant %s: %s", tenant_id, e)

        if tenant and tenant.status in ("suspended", "cancelled"):
            log.warning("Skipping task %s — tenant %s is %s", queue_id, tenant.slug, tenant.status)
            svc.update_queue_item(queue_id, status="failed",
                                  result_summary=f"Tenant {tenant.status}")
            return

        # Check usage limits
        if tenant:
            task_type = item.get("task_type", "bug")
            record_type = "feature" if task_type == "feature" else "bug_fix"
            allowed, reason = check_limits(tenant_id, record_type)
            if not allowed:
                log.warning("Usage limit reached for tenant %s: %s", tenant.slug, reason)
                svc.update_queue_item(queue_id, status="failed",
                                      result_summary=f"Limit reached: {reason}")
                svc.add_message(session_id, "system", "Worker",
                                f"Task skipped: {reason}",
                                message_type="error")
                return

    if phase == "understand":
        return process_understanding(svc, item, tenant=tenant)

    # === EXECUTION PHASE (multi-agent) ===
    # Get understanding_output from queue item or session
    understanding_output = item.get("understanding_output") or ""
    if not understanding_output:
        try:
            sess = svc.get_session(session_id)
            if sess:
                understanding_output = sess.get("understanding_output") or ""
        except Exception:
            pass

    return process_execution_multi(svc, item, understanding_output, tenant=tenant)


def _send_notifications(parsed, result, task_label, description, summary,
                        soak_passed, soak_output, elapsed_min, elapsed_sec,
                        session_id=None, verdict=""):
    """Send SMS and email notifications based on outcome.

    Uses assessor verdict as primary signal when available, falls back to
    marker-based parsing.
    """
    session_url = ""
    if session_id:
        session_url = " {base}/ai-ops/session/{sid}".format(
            base=APP_BASE_URL, sid=session_id,
        )

    stdout_tail = result.get("stdout", "")[-5000:] if result else ""

    app = config.APP_NAME

    # --- Verdict-based notifications (primary) ---
    if verdict == "FIXED":
        send_sms(
            f'{app} AI Ops: DONE (VERIFIED). '
            f'"{description[:60]}" fixed and deployed. '
            f'({elapsed_min}m {elapsed_sec}s){session_url}'
        )
        send_email(
            f"{app} AI Ops: FIXED -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Verdict: FIXED (verified by Assessor agent)\n"
            f"Summary: {summary}\n"
            f"Time: {elapsed_min}m {elapsed_sec}s\n\n"
            f"--- Agent Report ---\n{stdout_tail}",
        )

    elif verdict == "REGRESSION":
        send_sms(
            f'{app} AI Ops: REGRESSION detected for '
            f'"{description[:60]}". Auto-rolled back.{session_url}'
        )
        send_email(
            f"{app} AI Ops: REGRESSION -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Verdict: REGRESSION (fix introduced new issues)\n"
            f"Summary: {summary}\n\n"
            f"--- Agent Report ---\n{stdout_tail}",
        )

    elif verdict == "ESCALATE":
        send_sms(
            f'{app} AI Ops: NEEDS ATTENTION -- '
            f'"{description[:60]}" escalated by Assessor.{session_url}'
        )
        send_email(
            f"{app} AI Ops: ESCALATE -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Verdict: ESCALATE (needs human intervention)\n"
            f"Summary: {summary}\n\n"
            f"--- Agent Report ---\n{stdout_tail}",
        )

    elif verdict in ("PARTIAL", "FAILED"):
        send_sms(
            f'{app} AI Ops: {verdict} for '
            f'"{description[:60]}". Check portal.{session_url}'
        )
        send_email(
            f"{app} AI Ops: {verdict} -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Verdict: {verdict}\n"
            f"Summary: {summary}\n\n"
            f"--- Agent Report ---\n{stdout_tail}",
        )

    # --- Fallback: marker-based notifications (when no verdict) ---
    elif parsed.get("escalated"):
        esc_reason = parsed.get("escalation_reason", "Unknown")
        send_sms(
            f'{app} AI Ops: NEEDS ATTENTION -- '
            f'"{description[:60]}" escalated. '
            f'Reason: {esc_reason[:80]}{session_url}'
        )
        send_email(
            f"{app} AI Ops: Escalation -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Reason: {esc_reason}\n\n"
            f"Agent output (last 3000 chars):\n{stdout_tail[-3000:]}",
        )

    elif result and result.get("timed_out"):
        send_sms(
            f'{app} AI Ops: TIMED OUT on '
            f'"{description[:60]}". Check logs.{session_url}'
        )
        send_email(
            f"{app} AI Ops: Timeout -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Status: Agent timed out.\n"
            f"Summary: {summary}\n\n"
            f"Agent output (last 3000 chars):\n{stdout_tail[-3000:]}",
        )

    elif parsed.get("validated") and (parsed.get("smoke_passed") or soak_passed):
        send_sms(
            f'{app} AI Ops: DONE. '
            f'"{description[:60]}" fixed and deployed. '
            f'({elapsed_min}m {elapsed_sec}s){session_url}'
        )
        send_email(
            f"{app} AI Ops: Deployed -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Status: Fixed, validated, and deployed\n"
            f"Commit: {parsed.get('commit_sha', '')}\n"
            f"Time: {elapsed_min}m {elapsed_sec}s\n"
            f"Soak: {'PASS' if soak_passed else 'NOT RUN'}\n\n"
            f"--- Agent Report ---\n{stdout_tail}",
        )

    else:
        send_sms(
            f'{app} AI Ops: Outcome unclear for '
            f'"{description[:60]}". Check the portal.{session_url}'
        )
        send_email(
            f"{app} AI Ops: Review Needed -- {description[:60]}",
            f"Task: {description}\n"
            f"Type: {task_label}\n"
            f"Status: Agent finished but validation markers not found.\n"
            f"Summary: {summary}\n\n"
            f"Agent output (last 5000 chars):\n{stdout_tail}",
        )


# ---------------------------------------------------------------------------
# Stuck Task Recovery
# ---------------------------------------------------------------------------


def recover_stuck_tasks(svc):
    """On startup, find tasks stuck in 'running' for >45 minutes and mark failed."""
    try:
        result = svc.supabase.table("ai_ops_agent_queue") \
            .select("*") \
            .in_("status", ["running", "claimed"]) \
            .execute()

        stuck_items = result.data or []
        if not stuck_items:
            log.info("No stuck tasks found on startup.")
            return

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=STUCK_TASK_MINUTES)

        for item in stuck_items:
            picked_up = item.get("picked_up_at")
            if not picked_up:
                # No picked_up_at means it's been running with no timestamp -- mark failed
                log.warning(
                    "Stuck task %s has no picked_up_at, marking failed",
                    item["id"],
                )
                _mark_stuck_failed(svc, item)
                continue

            # Parse the timestamp -- handle varying fractional digit counts
            # Python 3.10.12 fromisoformat requires 0, 3, or 6 fractional digits
            try:
                # Strip trailing Z and replace with +00:00 if present
                ts_str = picked_up
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                picked_up_dt = datetime.fromisoformat(ts_str)
            except ValueError:
                # Fallback: try strptime with a common format
                try:
                    picked_up_dt = datetime.strptime(
                        picked_up[:19], "%Y-%m-%dT%H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    log.warning(
                        "Cannot parse picked_up_at '%s' for task %s, marking failed",
                        picked_up, item["id"],
                    )
                    _mark_stuck_failed(svc, item)
                    continue

            # Ensure timezone-aware comparison
            if picked_up_dt.tzinfo is None:
                picked_up_dt = picked_up_dt.replace(tzinfo=timezone.utc)

            if picked_up_dt < cutoff:
                age_min = int((now - picked_up_dt).total_seconds() / 60)
                log.warning(
                    "Stuck task %s: running for %d minutes (threshold: %d), marking failed",
                    item["id"], age_min, STUCK_TASK_MINUTES,
                )
                _mark_stuck_failed(svc, item)
            else:
                age_min = int((now - picked_up_dt).total_seconds() / 60)
                log.info(
                    "Task %s is running for %d minutes (under threshold), leaving it",
                    item["id"], age_min,
                )

    except Exception as e:
        log.error("Stuck task recovery error: %s", e)


def _mark_stuck_failed(svc, item):
    """Mark a stuck task as failed and update its session."""
    try:
        svc.update_queue_item(
            item["id"],
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            result_summary="Agent worker was restarted while task was running.",
        )
    except Exception as e:
        log.error("Failed to update stuck queue item %s: %s", item["id"], e)

    session_id = item.get("session_id")
    if session_id:
        try:
            svc.update_session(
                session_id,
                status="failed",
                summary="Agent worker was restarted while task was running.",
            )
            svc.add_message(
                session_id, "system", "Worker",
                "Task was interrupted by worker restart. Marked as failed. "
                "Please re-submit if needed.",
                message_type="error",
            )
        except Exception as e:
            log.error("Failed to update stuck session %s: %s", session_id, e)


# ---------------------------------------------------------------------------
# Orphaned Session Cleanup
# ---------------------------------------------------------------------------

ORPHANED_SESSION_MINUTES = 30  # Sessions stuck in gathering_info with no messages for 30+ min


def cleanup_orphaned_sessions(svc):
    """Find sessions stuck in gathering_info with no user activity and clean them up.

    Two cases:
    1. Sessions with a pending queue entry but status still gathering_info (bug intake race):
       fix by updating session status to 'queued'.
    2. Sessions with no messages and no queue entry for >30 min (abandoned):
       mark as 'failed' with explanation.
    """
    try:
        result = svc.supabase.table("ai_ops_sessions").select(
            "id,created_at,title,status"
        ).eq("status", "gathering_info").execute()

        sessions = result.data or []
        if not sessions:
            return

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=ORPHANED_SESSION_MINUTES)

        for sess in sessions:
            session_id = sess["id"]
            created_str = sess["created_at"]

            # Parse created_at
            try:
                ts = created_str
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                created_dt = datetime.fromisoformat(ts)
            except ValueError:
                try:
                    created_dt = datetime.strptime(
                        created_str[:19], "%Y-%m-%dT%H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)

            if created_dt >= cutoff:
                continue  # Too recent, skip

            # Check if there's a pending queue entry (fix race condition)
            q = svc.supabase.table("ai_ops_agent_queue").select(
                "id,status"
            ).eq("session_id", session_id).execute()
            queue_items = q.data or []

            if queue_items:
                # Has queue entry but session still in gathering_info — fix the status
                pending = any(qi["status"] in ("pending", "claimed", "running") for qi in queue_items)
                if pending:
                    log.info(
                        "Fixing orphaned session %s: has pending queue item, setting status to 'queued'",
                        session_id[:8],
                    )
                    svc.update_session(session_id, status="queued")
                continue

            # No queue entry — check for user messages
            m = svc.supabase.table("ai_ops_messages").select(
                "id", count="exact"
            ).eq("session_id", session_id).eq("sender_type", "user").execute()
            msg_count = m.count or 0

            if msg_count == 0:
                # Abandoned session (no messages, no queue, old)
                age_min = int((now - created_dt).total_seconds() / 60)
                log.info(
                    "Cleaning up abandoned session %s: %d min old, no messages",
                    session_id[:8], age_min,
                )
                svc.update_session(
                    session_id,
                    status="failed",
                    summary="Session abandoned (no messages after %d minutes)." % age_min,
                )

    except Exception as e:
        log.error("Orphaned session cleanup error: %s", e)


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------


def main():
    log.info("=" * 60)
    log.info("AI Ops Worker starting...")
    log.info("Poll interval: %ds", POLL_INTERVAL)
    log.info("Agent timeout: %ds", AGENT_TIMEOUT)
    log.info("Working directory: %s", WORKING_DIR)
    log.info("Protocol file: %s", PROTOCOL_FILE)
    log.info("=" * 60)

    # Verify Supabase credentials
    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("PROD_SUPABASE_URL")
    supabase_key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or os.getenv("PROD_SUPABASE_KEY")
    )
    if not supabase_url or not supabase_key:
        log.error(
            "Supabase credentials not found. "
            "Set SUPABASE_URL and SUPABASE_KEY in .env"
        )
        sys.exit(1)

    log.info("Connecting to Supabase: %s...", supabase_url[:40])

    svc = AIOpsService()

    # Recover stuck tasks from previous worker run
    recover_stuck_tasks(svc)

    log.info("Entering poll loop...")
    poll_count = 0

    while not shutdown:
        try:
            poll_count += 1

            # Write heartbeat for external monitoring
            try:
                with open(HEARTBEAT_FILE, "w") as hb:
                    hb.write(datetime.now(timezone.utc).isoformat())
            except Exception:
                pass

            item = svc.get_pending_queue_item()
            if item:
                log.info(
                    "Found pending task: %s (%s)",
                    item["id"], item.get("description", "")[:60],
                )
                process_task(svc, item)
            else:
                # Check bug intake queue every Nth cycle when idle
                if poll_count % BUG_CHECK_INTERVAL == 0:
                    check_bug_queue(svc)
                    cleanup_orphaned_sessions(svc)
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, shutting down.")
            break
        except Exception as e:
            log.error("Poll loop error: %s", e, exc_info=True)
            time.sleep(POLL_INTERVAL)

    log.info("AI Ops Worker shutting down.")


if __name__ == "__main__":
    main()
