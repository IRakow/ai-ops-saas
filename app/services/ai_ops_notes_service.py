"""
AI Ops Notes Service — CRUD for user feedback notes + Gemini batch analysis.
"""

import uuid
import logging
from datetime import datetime, timezone
from app.supabase_client import get_supabase_client, execute_with_retry
from app.services.gemini_client import get_gemini_client

logger = logging.getLogger(__name__)

ANALYSIS_SCHEMA = """{
  "groups": [
    {
      "theme": "string (3-8 words)",
      "summary": "string (2-4 sentences)",
      "priority": "High | Medium | Low",
      "category": "bug_fix | new_feature | ux_improvement | performance | other",
      "suggested_mode": "bug_fix | new_feature",
      "suggested_session_title": "string",
      "suggested_session_description": "string (detailed, actionable)",
      "note_indices": [0, 1, 2]
    }
  ]
}"""


class AIOpsNotesService:
    """CRUD + Gemini analysis for user feedback notes."""

    def __init__(self):
        self.supabase = get_supabase_client()

    # =========================================================================
    # NOTES CRUD
    # =========================================================================

    def submit_note(self, content: str, submitter_name: str = None,
                    submitter_email: str = None, submitter_id: str = None,
                    page_url: str = None, page_title: str = None,
                    metadata: dict = None, tenant_id: str = None) -> dict | None:
        """Insert a new feedback note."""
        row = {
            "content": content.strip(),
            "submitter_name": submitter_name or "Anonymous",
            "submitter_email": submitter_email,
            "submitter_id": submitter_id,
            "page_url": page_url,
            "page_title": page_title,
            "status": "unreviewed",
            "metadata": metadata or {},
        }
        if tenant_id:
            row["tenant_id"] = tenant_id
        try:
            result = self.supabase.table("ai_ops_notes").insert(row).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Failed to submit note: {e}")
            return None

    def list_notes(self, status: str = None, limit: int = 200,
                   tenant_id: str = None) -> list:
        """List notes ordered by created_at DESC, optional status filter."""
        query = self.supabase.table("ai_ops_notes") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(limit)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        if status:
            query = query.eq("status", status)
        result = execute_with_retry(lambda: query.execute())
        return result.data or []

    def count_unreviewed(self, tenant_id: str = None) -> int:
        """Count notes with status='unreviewed'."""
        try:
            query = self.supabase.table("ai_ops_notes") \
                .select("id", count="exact") \
                .eq("status", "unreviewed")
            if tenant_id:
                query = query.eq("tenant_id", tenant_id)
            result = execute_with_retry(lambda: query.execute())
            return result.count or 0
        except Exception:
            return 0

    def update_note_status(self, note_id: str, status: str,
                           suggestion_id: str = None,
                           session_id: str = None) -> dict | None:
        """Update a note's status and optional FK linkage."""
        update = {
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if suggestion_id:
            update["suggestion_id"] = suggestion_id
        if session_id:
            update["session_id"] = session_id
        try:
            result = self.supabase.table("ai_ops_notes") \
                .update(update).eq("id", note_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Failed to update note {note_id}: {e}")
            return None

    # =========================================================================
    # GEMINI BATCH ANALYSIS
    # =========================================================================

    def analyze_notes(self, tenant_id: str = None) -> list:
        """Fetch unreviewed notes, send to Gemini for clustering, save suggestions."""
        notes = self.list_notes(status="unreviewed", limit=500, tenant_id=tenant_id)
        if not notes:
            return []

        batch_id = str(uuid.uuid4())

        # Build prompt
        notes_text = []
        for i, n in enumerate(notes):
            date_str = n.get("created_at", "")[:10] if n.get("created_at") else "unknown"
            notes_text.append(
                f'[{i}] From: {n.get("submitter_name", "Anonymous")} '
                f'| Page: {n.get("page_url", "N/A")} '
                f'| Date: {date_str}\n'
                f'    "{n.get("content", "")}"'
            )

        prompt = f"""Analyze these {len(notes)} user feedback notes from a property management SaaS platform.
The platform handles leasing, maintenance, accounting, properties, tenants, owners, vendors, and communications.

GROUP related notes by theme. For each group provide:
1. theme: short name (3-8 words)
2. summary: 2-4 sentences on what users are saying
3. priority: "High" (blocking/broken), "Medium" (significant pain), "Low" (nice-to-have)
4. category: bug_fix | new_feature | ux_improvement | performance | other
5. suggested_mode: bug_fix or new_feature
6. suggested_session_title: concise title for an engineering session
7. suggested_session_description: detailed description an AI agent could act on, including affected pages, expected behavior, acceptance criteria
8. note_indices: which notes (0-based) belong to this group

RULES:
- Each note belongs to exactly one group
- Every note must be assigned (create "Miscellaneous" for orphans)
- Prioritize issues affecting multiple users or core workflows

NOTES:
{chr(10).join(notes_text)}"""

        system_instruction = (
            "You are an expert product analyst specializing in property management software. "
            "Analyze user feedback and group it into actionable engineering sessions. "
            "Be specific in session descriptions — mention affected pages, expected behavior, "
            "and acceptance criteria so an AI agent can implement the fix or feature."
        )

        gemini = get_gemini_client()
        analysis = gemini.generate_json(
            prompt=prompt,
            system_instruction=system_instruction,
            schema_hint=ANALYSIS_SCHEMA,
        )

        if not analysis or "groups" not in analysis:
            logger.error("Gemini analysis returned no groups")
            return []

        # Save suggestions and link notes
        suggestions = []
        for group in analysis["groups"]:
            note_indices = group.get("note_indices", [])
            group_note_ids = []
            excerpts = []
            for idx in note_indices:
                if 0 <= idx < len(notes):
                    note = notes[idx]
                    group_note_ids.append(note["id"])
                    excerpts.append({
                        "name": note.get("submitter_name", "Anonymous"),
                        "excerpt": (note.get("content", ""))[:200],
                    })

            row = {
                "theme": group.get("theme", "Untitled")[:500],
                "summary": group.get("summary", ""),
                "priority": group.get("priority", "Medium"),
                "category": group.get("category", "other"),
                "suggested_mode": group.get("suggested_mode", "new_feature"),
                "suggested_session_title": group.get("suggested_session_title", "")[:500],
                "suggested_session_description": group.get("suggested_session_description", ""),
                "note_ids": group_note_ids,
                "related_note_excerpts": excerpts,
                "status": "pending",
                "analysis_batch_id": batch_id,
            }
            if tenant_id:
                row["tenant_id"] = tenant_id

            try:
                result = self.supabase.table("ai_ops_note_suggestions").insert(row).execute()
                if result.data:
                    suggestion = result.data[0]
                    suggestions.append(suggestion)

                    # Mark linked notes as reviewed
                    for nid in group_note_ids:
                        self.update_note_status(nid, "reviewed", suggestion_id=suggestion["id"])
            except Exception as e:
                logger.error(f"Failed to save suggestion '{group.get('theme')}': {e}")

        return suggestions

    # =========================================================================
    # SUGGESTIONS
    # =========================================================================

    def list_suggestions(self, status: str = None, limit: int = 50,
                         tenant_id: str = None) -> list:
        """List suggestions ordered by priority."""
        priority_order = {"High": 1, "Medium": 2, "Low": 3}
        query = self.supabase.table("ai_ops_note_suggestions") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(limit)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        if status:
            query = query.eq("status", status)
        result = execute_with_retry(lambda: query.execute())
        suggestions = result.data or []
        # Sort by priority in Python since Supabase can't sort by custom order
        suggestions.sort(key=lambda s: priority_order.get(s.get("priority", "Low"), 3))
        return suggestions

    def dismiss_suggestion(self, suggestion_id: str) -> dict | None:
        """Mark a suggestion as dismissed."""
        try:
            result = self.supabase.table("ai_ops_note_suggestions") \
                .update({"status": "dismissed"}) \
                .eq("id", suggestion_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Failed to dismiss suggestion {suggestion_id}: {e}")
            return None

    def promote_to_session(self, suggestion_id: str, ai_ops_user_id: str,
                           tenant_id: str = None) -> dict | None:
        """Create an AI Ops session from a suggestion, link notes, return session dict."""
        from app.services.ai_ops_service import AIOpsService

        # 1. Read suggestion
        try:
            result = execute_with_retry(
                lambda: self.supabase.table("ai_ops_note_suggestions")
                .select("*")
                .eq("id", suggestion_id)
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            suggestion = result.data[0]
        except Exception as e:
            logger.error(f"Failed to fetch suggestion {suggestion_id}: {e}")
            return None

        if suggestion["status"] != "pending":
            logger.warning(f"Suggestion {suggestion_id} is {suggestion['status']}, not pending")
            return None

        # 2. Fetch original notes
        note_ids = suggestion.get("note_ids", [])
        notes_text = []
        if note_ids:
            try:
                result = execute_with_retry(
                    lambda: self.supabase.table("ai_ops_notes")
                    .select("*")
                    .in_("id", note_ids)
                    .execute()
                )
                for note in (result.data or []):
                    notes_text.append(
                        f"- **{note.get('submitter_name', 'Anonymous')}** "
                        f"(from {note.get('page_url', 'unknown page')}): "
                        f"\"{note.get('content', '')}\""
                    )
            except Exception as e:
                logger.error(f"Failed to fetch notes for suggestion: {e}")

        # 3. Create session
        svc = AIOpsService()
        mode = suggestion.get("suggested_mode", "new_feature")
        title = suggestion.get("suggested_session_title", suggestion["theme"])
        new_session = svc.create_session(ai_ops_user_id, mode, title=title,
                                         tenant_id=tenant_id)
        if not new_session:
            logger.error("Failed to create session from suggestion")
            return None

        session_id = new_session["id"]

        # 4. Add context message with all note details
        context = (
            f"## Session Created from User Feedback Analysis\n\n"
            f"**Theme:** {suggestion['theme']}\n"
            f"**Priority:** {suggestion['priority']}\n"
            f"**Category:** {suggestion.get('category', 'other')}\n\n"
            f"### Summary\n{suggestion['summary']}\n\n"
            f"### Detailed Description\n{suggestion.get('suggested_session_description', '')}\n\n"
            f"### Original User Notes ({len(notes_text)})\n"
            + "\n".join(notes_text)
        )
        svc.add_message(session_id, "system", "System", context, message_type="context")

        # 5. Update suggestion
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.supabase.table("ai_ops_note_suggestions").update({
                "status": "promoted",
                "promoted_session_id": session_id,
                "promoted_by": ai_ops_user_id,
                "promoted_at": now,
            }).eq("id", suggestion_id).execute()
        except Exception as e:
            logger.error(f"Failed to update suggestion after promotion: {e}")

        # 6. Update all linked notes
        for nid in note_ids:
            self.update_note_status(nid, "actioned",
                                    suggestion_id=suggestion_id,
                                    session_id=session_id)

        # 7. Audit
        svc.log_audit(session_id, ai_ops_user_id, "promoted_from_suggestion", {
            "suggestion_id": suggestion_id,
            "theme": suggestion["theme"],
        })

        return new_session
