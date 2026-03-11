"""
Bug Intake Service — Dedup, screenshot upload, status tracking for auto-detected
and user-reported bugs that feed into the AI Ops pipeline.
"""

import base64
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta

from app.supabase_client import get_supabase_client

log = logging.getLogger(__name__)

# Dedup window: same error fingerprint within this period is merged
DEDUP_WINDOW_MINUTES = 30

# Statuses that mean "already handled" — don't merge into these
TERMINAL_STATUSES = ("fixed", "deployed")


class BugIntakeService:
    def __init__(self):
        self.supabase = get_supabase_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_report(self, data: dict) -> dict:
        """
        Main entry point. Accepts a bug report dict from the frontend,
        deduplicates, uploads screenshot, and stores in bug_reports.

        Returns: {bug_id, is_duplicate, status, session_token}
        """
        fingerprint = self._generate_fingerprint(
            data.get("error_message", ""),
            data.get("url_path", ""),
        )
        environment = data.get("environment", "production")

        # Check for existing duplicate
        existing = self._find_duplicate(fingerprint, environment)
        if existing:
            self._merge_duplicate(existing["id"])
            return {
                "bug_id": existing["id"],
                "is_duplicate": True,
                "status": existing["status"],
                "session_token": existing["reporter_session_token"],
            }

        # Upload screenshot if present
        screenshot_url = None
        screenshot_path = None
        if data.get("screenshot_base64"):
            try:
                screenshot_url, screenshot_path = self._upload_screenshot(
                    data["screenshot_base64"]
                )
            except Exception as e:
                log.warning("Screenshot upload failed: %s", e)

        # Insert new bug report
        row = {
            "source": data.get("source", "auto_detect"),
            "reporter_user_id": data.get("reporter_user_id"),
            "error_fingerprint": fingerprint,
            "error_type": data.get("error_type"),
            "error_message": (data.get("error_message") or "")[:4000],
            "url_path": data.get("url_path"),
            "js_stack_trace": (data.get("js_stack_trace") or "")[:8000],
            "http_status": data.get("http_status"),
            "user_description": (data.get("user_description") or "")[:2000],
            "console_log_tail": data.get("console_log_tail", []),
            "network_errors": data.get("network_errors", []),
            "local_storage_snapshot": data.get("local_storage_snapshot", {}),
            "page_html_snippet": (data.get("page_html_snippet") or "")[:5000],
            "user_agent": data.get("user_agent"),
            "viewport": data.get("viewport"),
            "screenshot_gcs_url": screenshot_url,
            "screenshot_gcs_path": screenshot_path,
            "environment": environment,
            "status": "new",
        }

        result = self.supabase.table("bug_reports").insert(row).execute()
        record = result.data[0] if result.data else None
        if not record:
            raise RuntimeError("Failed to insert bug report")

        log.info(
            "Bug report created: id=%s, type=%s, path=%s, env=%s",
            record["id"], record["error_type"], record["url_path"], environment,
        )

        return {
            "bug_id": record["id"],
            "is_duplicate": False,
            "status": "new",
            "session_token": record["reporter_session_token"],
        }

    def get_status(self, bug_id: str = None, session_token: str = None) -> list:
        """Get bug report status(es) by bug_id or session_token."""
        if bug_id:
            result = (
                self.supabase.table("bug_reports")
                .select("id, status, status_message, error_type, url_path, updated_at")
                .eq("id", bug_id)
                .execute()
            )
            return result.data or []

        if session_token:
            result = (
                self.supabase.table("bug_reports")
                .select("id, status, status_message, error_type, url_path, updated_at")
                .eq("reporter_session_token", session_token)
                .order("created_at", desc=True)
                .limit(20)
                .execute()
            )
            return result.data or []

        return []

    def get_new_reports(self, limit: int = 3) -> list:
        """Fetch bug reports with status='new' for worker pickup."""
        result = (
            self.supabase.table("bug_reports")
            .select("*")
            .eq("status", "new")
            .order("created_at")
            .limit(limit)
            .execute()
        )
        return result.data or []

    def link_to_ai_ops(self, bug_id: str, session_id: str, queue_id: str = None):
        """Link a bug report to an AI Ops session and mark as queued."""
        update = {
            "ai_ops_session_id": session_id,
            "status": "queued",
            "status_message": "Bug queued for AI analysis.",
        }
        if queue_id:
            update["ai_ops_queue_id"] = queue_id
        self.supabase.table("bug_reports").update(update).eq("id", bug_id).execute()

    def update_status(self, bug_id: str, status: str, message: str = None):
        """Update bug report status and optional user-facing message."""
        update = {"status": status}
        if message:
            update["status_message"] = message
        self.supabase.table("bug_reports").update(update).eq("id", bug_id).execute()
        log.info("Bug %s status → %s: %s", bug_id, status, message or "")

    def find_bug_by_session(self, ai_ops_session_id: str) -> dict | None:
        """Find the bug report linked to an AI Ops session."""
        result = (
            self.supabase.table("bug_reports")
            .select("id, status, environment")
            .eq("ai_ops_session_id", ai_ops_session_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_fingerprint(self, error_message: str, url_path: str) -> str:
        """
        Normalize error message (strip volatile parts) + URL path → SHA-256.
        This groups the same root-cause error together.
        """
        msg = error_message or ""
        # Strip line/column numbers (e.g., ":123:45")
        msg = re.sub(r":\d+:\d+", ":X:X", msg)
        # Strip UUIDs
        msg = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "<UUID>", msg, flags=re.IGNORECASE,
        )
        # Strip hex addresses (e.g., 0x7fff1234)
        msg = re.sub(r"0x[0-9a-fA-F]+", "<HEX>", msg)
        # Strip numeric IDs after /path/
        msg = re.sub(r"/\d+", "/<ID>", msg)

        path = (url_path or "").split("?")[0]  # strip query params
        raw = f"{msg.strip().lower()}|{path.strip().lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _find_duplicate(self, fingerprint: str, environment: str) -> dict | None:
        """Find an existing bug with same fingerprint+env within the dedup window."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=DEDUP_WINDOW_MINUTES)
        ).isoformat()

        result = (
            self.supabase.table("bug_reports")
            .select("id, status, reporter_session_token")
            .eq("error_fingerprint", fingerprint)
            .eq("environment", environment)
            .not_.in_("status", TERMINAL_STATUSES)
            .gte("last_seen_at", cutoff)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def _merge_duplicate(self, bug_id: str):
        """Increment occurrence count and bump last_seen_at on duplicate."""
        # Supabase doesn't support atomic increment easily, so read-then-write
        result = (
            self.supabase.table("bug_reports")
            .select("occurrence_count")
            .eq("id", bug_id)
            .single()
            .execute()
        )
        current = result.data.get("occurrence_count", 1) if result.data else 1
        self.supabase.table("bug_reports").update({
            "occurrence_count": current + 1,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", bug_id).execute()
        log.info("Bug %s: merged duplicate (count=%d)", bug_id, current + 1)

    def _upload_screenshot(self, base64_data: str) -> tuple[str, str]:
        """Decode base64 PNG and upload to GCS. Returns (url, path) or (None, None) if GCS unavailable."""
        try:
            from app.services.gcs_service import GCSService
        except ImportError:
            log.warning("GCSService not available, skipping screenshot upload")
            return None, None

        # Strip data URI prefix if present
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]

        image_bytes = base64.b64decode(base64_data)
        filename = f"bug-screenshot-{uuid.uuid4().hex[:12]}.png"
        blob_path = f"bug-screenshots/{filename}"

        gcs = GCSService()
        blob = gcs.bucket.blob(blob_path)
        blob.upload_from_string(image_bytes, content_type="image/png")

        url = f"https://storage.googleapis.com/{gcs.bucket_name}/{blob_path}"
        log.info("Screenshot uploaded: %s (%d bytes)", blob_path, len(image_bytes))
        return url, blob_path
