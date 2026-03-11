"""
AI Ops Service — Supabase CRUD for the AI Operations panel.
Handles sessions, messages, tasks, files, audit log, and user management.
"""

import logging
import bcrypt
from datetime import datetime, timezone
from app.supabase_client import get_supabase_client, execute_with_retry

logger = logging.getLogger(__name__)


class AIOpsService:
    """Core CRUD service for AI Ops panel data."""

    def __init__(self):
        self.supabase = get_supabase_client()

    # =========================================================================
    # USERS
    # =========================================================================

    def authenticate_user(self, email, password, tenant_id=None):
        """Authenticate an AI Ops user by email and password."""
        try:
            query = self.supabase.table("ai_ops_users") \
                .select("*") \
                .eq("email", email.lower().strip()) \
                .eq("is_active", True)
            if tenant_id:
                query = query.eq("tenant_id", tenant_id)
            result = execute_with_retry(
                lambda: query.limit(1).execute()
            )
            if not result.data:
                return None

            user = result.data[0]
            stored_hash = user["password_hash"]

            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode("utf-8")

            if bcrypt.checkpw(password.encode("utf-8"), stored_hash):
                # Update last login
                self.supabase.table("ai_ops_users").update({
                    "last_login_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", user["id"]).execute()
                return user

            return None
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return None

    def get_user(self, user_id, tenant_id=None):
        """Get a user by ID."""
        query = self.supabase.table("ai_ops_users") \
            .select("id, name, email, phone, is_active, last_login_at") \
            .eq("id", user_id)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        result = execute_with_retry(
            lambda: query.limit(1).execute()
        )
        return result.data[0] if result.data else None

    def create_user(self, name, email, password, phone=None, tenant_id=None):
        """Create a new AI Ops user with bcrypt-hashed password."""
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        row = {
            "name": name,
            "email": email.lower().strip(),
            "phone": phone,
            "password_hash": password_hash,
            "is_active": True,
        }
        if tenant_id:
            row["tenant_id"] = tenant_id

        result = self.supabase.table("ai_ops_users").insert(row).execute()

        return result.data[0] if result.data else None

    # =========================================================================
    # SESSIONS
    # =========================================================================

    def create_session(self, user_id, mode, title=None, tenant_id=None):
        """Create a new AI Ops session."""
        row = {
            "user_id": user_id,
            "mode": mode,
            "title": title,
            "status": "gathering_info",
        }
        if tenant_id:
            row["tenant_id"] = tenant_id
        result = self.supabase.table("ai_ops_sessions").insert(row).execute()

        session_data = result.data[0] if result.data else None
        if session_data:
            self.log_audit(session_data["id"], user_id, "session_created", {
                "mode": mode
            })
        return session_data

    def get_session(self, session_id, tenant_id=None):
        """Get a session by ID."""
        query = self.supabase.table("ai_ops_sessions") \
            .select("*, ai_ops_users(name, email)") \
            .eq("id", session_id)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        result = execute_with_retry(
            lambda: query.limit(1).execute()
        )
        return result.data[0] if result.data else None

    def list_sessions(self, user_id=None, status=None, date_from=None,
                       date_to=None, limit=20, tenant_id=None):
        """List sessions with optional filters."""
        query = self.supabase.table("ai_ops_sessions") \
            .select("*, ai_ops_users(name, email)") \
            .order("created_at", desc=True) \
            .limit(limit)

        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        if user_id:
            query = query.eq("user_id", user_id)
        if status:
            query = query.eq("status", status)
        if date_from:
            query = query.gte("created_at", f"{date_from}T00:00:00")
        if date_to:
            query = query.lte("created_at", f"{date_to}T23:59:59")

        result = execute_with_retry(lambda: query.execute())
        return result.data or []

    def list_attention_sessions(self, tenant_id=None):
        """Return sessions that need user action (approvals, failures, escalations)."""
        attention_statuses = [
            "awaiting_approval", "awaiting_test_approval",
            "failed", "needs_review", "escalated", "rolled_back",
        ]
        query = self.supabase.table("ai_ops_sessions") \
            .select("*, ai_ops_users(name, email)") \
            .in_("status", attention_statuses) \
            .order("updated_at", desc=True)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        result = execute_with_retry(lambda: query.execute())
        return result.data or []

    def list_users(self, tenant_id=None):
        """Return all active AI Ops users (for filter dropdowns)."""
        query = self.supabase.table("ai_ops_users") \
            .select("id, name, email") \
            .eq("is_active", True) \
            .order("name", desc=False)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        result = execute_with_retry(lambda: query.execute())
        return result.data or []

    def update_session(self, session_id, **kwargs):
        """Update session fields."""
        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = self.supabase.table("ai_ops_sessions") \
            .update(kwargs) \
            .eq("id", session_id) \
            .execute()
        return result.data[0] if result.data else None

    def update_session_status(self, session_id, status, user_id=None):
        """Update session status and log audit event."""
        result = self.update_session(session_id, status=status)
        self.log_audit(session_id, user_id, f"status_changed_to_{status}")
        return result

    # =========================================================================
    # MESSAGES
    # =========================================================================

    def add_message(self, session_id, sender_type, sender_name, content,
                    message_type="chat", metadata=None, tenant_id=None):
        """Add a message to a session."""
        row = {
            "session_id": session_id,
            "sender_type": sender_type,
            "sender_name": sender_name,
            "content": content,
            "message_type": message_type,
            "metadata": metadata or {},
        }
        if tenant_id:
            row["tenant_id"] = tenant_id
        result = self.supabase.table("ai_ops_messages").insert(row).execute()
        return result.data[0] if result.data else None

    def get_messages(self, session_id, after_id=None, limit=100,
                     exclude_types=None):
        """Get messages for a session, optionally after a specific message.

        Args:
            exclude_types: list of message_type values to filter out (e.g. ["plan"])
        """
        query = self.supabase.table("ai_ops_messages") \
            .select("*") \
            .eq("session_id", session_id) \
            .order("created_at", desc=False) \
            .limit(limit)

        if after_id:
            # Get the timestamp of the after_id message, then filter
            ref = self.supabase.table("ai_ops_messages") \
                .select("created_at") \
                .eq("id", after_id) \
                .limit(1) \
                .execute()
            if ref.data:
                query = query.gt("created_at", ref.data[0]["created_at"])

        result = execute_with_retry(lambda: query.execute())
        messages = result.data or []

        if exclude_types:
            messages = [
                m for m in messages
                if m.get("message_type") not in exclude_types
            ]

        return messages

    # =========================================================================
    # TASKS (Plan Items)
    # =========================================================================

    def create_tasks(self, session_id, tasks, tenant_id=None):
        """Create multiple task plan items for a session."""
        rows = []
        for i, task in enumerate(tasks, 1):
            row = {
                "session_id": session_id,
                "task_number": task.get("task_number", i),
                "title": task["title"],
                "description": task.get("description", ""),
                "status": "pending",
            }
            if tenant_id:
                row["tenant_id"] = tenant_id
            rows.append(row)

        result = self.supabase.table("ai_ops_tasks") \
            .insert(rows) \
            .execute()
        return result.data or []

    def get_tasks(self, session_id):
        """Get all tasks for a session, ordered by task_number."""
        result = execute_with_retry(
            lambda: self.supabase.table("ai_ops_tasks")
            .select("*")
            .eq("session_id", session_id)
            .order("task_number", desc=False)
            .execute()
        )
        return result.data or []

    def update_task(self, task_id, **kwargs):
        """Update a task's status, files_changed, test_results, etc."""
        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = self.supabase.table("ai_ops_tasks") \
            .update(kwargs) \
            .eq("id", task_id) \
            .execute()
        return result.data[0] if result.data else None

    # =========================================================================
    # FILES
    # =========================================================================

    def add_file(self, session_id, filename, gcs_path=None, gcs_url=None,
                  tenant_id=None):
        """Record an uploaded file."""
        row = {
            "session_id": session_id,
            "filename": filename,
            "gcs_path": gcs_path,
            "gcs_url": gcs_url,
        }
        if tenant_id:
            row["tenant_id"] = tenant_id
        result = self.supabase.table("ai_ops_files").insert(row).execute()
        return result.data[0] if result.data else None

    def get_files(self, session_id):
        """Get all files for a session."""
        result = execute_with_retry(
            lambda: self.supabase.table("ai_ops_files")
            .select("*")
            .eq("session_id", session_id)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data or []

    # =========================================================================
    # AUDIT LOG
    # =========================================================================

    def log_audit(self, session_id, user_id, event_type, details=None,
                   tenant_id=None):
        """Write an audit log entry."""
        try:
            row = {
                "session_id": session_id,
                "user_id": user_id,
                "event_type": event_type,
                "details": details or {},
            }
            if tenant_id:
                row["tenant_id"] = tenant_id
            self.supabase.table("ai_ops_audit_log").insert(row).execute()
        except Exception as e:
            logger.warning(f"Failed to write audit log: {e}")

    def get_audit_log(self, session_id=None, limit=50, tenant_id=None):
        """Get audit log entries."""
        query = self.supabase.table("ai_ops_audit_log") \
            .select("*, ai_ops_users(name)") \
            .order("created_at", desc=True) \
            .limit(limit)

        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        if session_id:
            query = query.eq("session_id", session_id)

        result = execute_with_retry(lambda: query.execute())
        return result.data or []

    # =========================================================================
    # AGENT QUEUE
    # =========================================================================

    def queue_task(self, session_id, task_type, description, attachments=None,
                   phase="execute", understanding_output=None, tenant_id=None):
        """Insert a task into the agent queue for the background worker."""
        row = {
            "session_id": session_id,
            "task_type": task_type,
            "description": description,
            "attachments": attachments or [],
            "priority": 0,
            "status": "pending",
            "phase": phase,
        }
        if understanding_output:
            row["understanding_output"] = understanding_output
        if tenant_id:
            row["tenant_id"] = tenant_id
        result = self.supabase.table("ai_ops_agent_queue").insert(row).execute()
        return result.data[0] if result.data else None

    def get_pending_queue_item(self, tenant_id=None):
        """Get and atomically claim the next pending queue item.

        Uses a SELECT then UPDATE with status='pending' filter to prevent
        double-pickup if multiple workers ever run concurrently.
        """
        query = self.supabase.table("ai_ops_agent_queue") \
            .select("*") \
            .eq("status", "pending") \
            .order("priority", desc=True) \
            .order("created_at", desc=False) \
            .limit(1)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        result = execute_with_retry(lambda: query.execute())
        if not result.data:
            return None

        item = result.data[0]

        # Atomic claim: only update if still pending (prevents race condition)
        claim_result = self.supabase.table("ai_ops_agent_queue") \
            .update({"status": "claimed"}) \
            .eq("id", item["id"]) \
            .eq("status", "pending") \
            .execute()

        if not claim_result.data:
            # Another worker claimed it first
            return None

        # Return the item with claimed status
        item["status"] = "claimed"
        return item

    def update_queue_item(self, queue_id, **kwargs):
        """Update a queue item's status and fields."""
        result = self.supabase.table("ai_ops_agent_queue") \
            .update(kwargs) \
            .eq("id", queue_id) \
            .execute()
        return result.data[0] if result.data else None
