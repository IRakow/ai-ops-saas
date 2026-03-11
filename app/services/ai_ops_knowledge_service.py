"""
AI Ops Knowledge Service — Fix Pattern storage and retrieval.
Stores every successful fix so future similar bugs get context from past solutions.

SAFETY: This service reads/writes the ai_ops_fix_patterns table on the
TEST Supabase only. The constructor validates that the client URL does not
point to the production project.
"""

import config
import logging
import os
import re
from datetime import datetime, timezone
from app.supabase_client import get_supabase_client, execute_with_retry

logger = logging.getLogger(__name__)

_PRODUCTION_SUPABASE_REF = config.PRODUCTION_SUPABASE_REF

# Common stop words to exclude when generating keywords for search
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "that", "this",
    "it", "its", "i", "me", "my", "we", "our", "you", "your", "he", "him",
    "his", "she", "her", "they", "them", "their", "what", "which", "who",
})


class AIOpsKnowledgeService:
    """Service for storing and retrieving fix patterns from the knowledge base."""

    def __init__(self):
        # SAFETY: Verify we are NOT hitting production Supabase
        supabase_url = os.getenv("SUPABASE_URL", "")
        if _PRODUCTION_SUPABASE_REF in supabase_url.lower():
            raise RuntimeError(
                f"SAFETY BLOCK: Knowledge service refusing to connect to "
                f"production Supabase ({_PRODUCTION_SUPABASE_REF})."
            )
        self.supabase = get_supabase_client()

    # =========================================================================
    # STORE
    # =========================================================================

    def store_fix_pattern(self, session_id, module, bug_summary, root_cause,
                          fix_description, files_changed, diff_summary=None,
                          resolution_time_seconds=None, tags=None,
                          tenant_id=None):
        """Store a successful fix pattern in the knowledge base.

        Auto-generates tags from module name and keywords in bug_summary if
        none are supplied.
        """
        if tags is None:
            tags = self._auto_generate_tags(module, bug_summary)

        row = {
            "session_id": session_id,
            "module": module,
            "bug_summary": bug_summary,
            "root_cause": root_cause,
            "fix_description": fix_description,
            "files_changed": files_changed or [],
            "diff_summary": diff_summary,
            "resolution_time_seconds": resolution_time_seconds,
            "success": True,
            "tags": tags,
        }
        if tenant_id:
            row["tenant_id"] = tenant_id

        try:
            result = self.supabase.table("ai_ops_fix_patterns") \
                .insert(row) \
                .execute()
            pattern = result.data[0] if result.data else None
            if pattern:
                logger.info(
                    f"Stored fix pattern {pattern['id']} for module={module}"
                )
            return pattern
        except Exception as e:
            logger.error(f"Failed to store fix pattern: {e}")
            return None

    # =========================================================================
    # SEARCH
    # =========================================================================

    def find_similar_patterns(self, bug_description, module=None, limit=3,
                              tenant_id=None):
        """Find past fix patterns similar to a given bug description.

        Uses keyword-based ILIKE matching against bug_summary and root_cause.
        Optionally filters by module. Results are ordered so patterns matching
        more keywords appear first (basic relevance ranking).
        """
        keywords = self._extract_keywords(bug_description)
        if not keywords:
            return []

        try:
            # Build an OR filter: each keyword checked against bug_summary
            # and root_cause via ILIKE.
            or_clauses = []
            for kw in keywords:
                pattern = f"%{kw}%"
                or_clauses.append(f"bug_summary.ilike.{pattern}")
                or_clauses.append(f"root_cause.ilike.{pattern}")

            or_filter = ",".join(or_clauses)

            query = self.supabase.table("ai_ops_fix_patterns") \
                .select("*") \
                .eq("success", True) \
                .or_(or_filter)

            if tenant_id:
                query = query.eq("tenant_id", tenant_id)
            if module:
                query = query.eq("module", module)

            query = query.order("created_at", desc=True) \
                .limit(limit * 3)  # over-fetch for relevance sorting

            result = execute_with_retry(lambda: query.execute())
            patterns = result.data or []

            # Rank by number of keyword hits across bug_summary + root_cause
            ranked = self._rank_by_relevance(patterns, keywords)
            return ranked[:limit]

        except Exception as e:
            logger.error(f"Failed to find similar patterns: {e}")
            return []

    def get_patterns_for_module(self, module, limit=10, tenant_id=None):
        """Get recent fix patterns for a specific module."""
        try:
            query = self.supabase.table("ai_ops_fix_patterns") \
                .select("*") \
                .eq("module", module) \
                .eq("success", True) \
                .order("created_at", desc=True) \
                .limit(limit)
            if tenant_id:
                query = query.eq("tenant_id", tenant_id)
            result = execute_with_retry(lambda: query.execute())
            return result.data or []
        except Exception as e:
            logger.error(f"Failed to get patterns for module {module}: {e}")
            return []

    # =========================================================================
    # FORMAT
    # =========================================================================

    def format_patterns_for_prompt(self, patterns):
        """Format a list of fix patterns as context for a Claude prompt.

        Returns a human-readable block like:

            PAST SIMILAR FIXES:

            Fix #1 (maintenance module, 2 days ago):
            Bug: "Priority dropdown empty on new work order page"
            Root Cause: Route handler not passing priority options to template
            Files Changed: app/routes/maintenance.py, ...
            Resolution: Added priority_options list to render_template call
        """
        if not patterns:
            return ""

        now = datetime.now(timezone.utc)
        lines = ["PAST SIMILAR FIXES:", ""]

        for i, p in enumerate(patterns, 1):
            age_str = self._friendly_age(p.get("created_at"), now)
            module = p.get("module", "unknown")
            lines.append(f"Fix #{i} ({module} module, {age_str}):")
            lines.append(f"Bug: \"{p.get('bug_summary', 'N/A')}\"")

            if p.get("root_cause"):
                lines.append(f"Root Cause: {p['root_cause']}")

            files = p.get("files_changed") or []
            if files:
                lines.append(f"Files Changed: {', '.join(files)}")

            if p.get("fix_description"):
                lines.append(f"Resolution: {p['fix_description']}")

            lines.append("")  # blank line between entries

        return "\n".join(lines).rstrip()

    # =========================================================================
    # PRIVATE HELPERS
    # =========================================================================

    def _auto_generate_tags(self, module, bug_summary):
        """Generate tags from module name and keywords in bug_summary."""
        tags = [module.lower()]
        keywords = self._extract_keywords(bug_summary)
        # Take up to 5 most distinctive keywords as tags
        for kw in keywords[:5]:
            if kw not in tags:
                tags.append(kw)
        return tags

    def _extract_keywords(self, text):
        """Extract meaningful keywords from text, filtering stop words."""
        if not text:
            return []
        # Lowercase, split on non-alphanumeric, filter short/stop words
        words = re.findall(r"[a-z0-9_]+", text.lower())
        return [w for w in words if len(w) >= 3 and w not in _STOP_WORDS]

    def _rank_by_relevance(self, patterns, keywords):
        """Rank patterns by number of keyword matches in bug_summary + root_cause."""

        def score(pattern):
            text = (
                (pattern.get("bug_summary") or "") + " " +
                (pattern.get("root_cause") or "")
            ).lower()
            return sum(1 for kw in keywords if kw in text)

        return sorted(patterns, key=score, reverse=True)

    def _friendly_age(self, created_at_str, now):
        """Convert a created_at timestamp to a friendly age string."""
        if not created_at_str:
            return "unknown time ago"
        try:
            # Handle ISO format with or without timezone
            created = datetime.fromisoformat(
                created_at_str.replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)

            delta = now - created
            days = delta.days
            if days == 0:
                hours = delta.seconds // 3600
                if hours == 0:
                    minutes = delta.seconds // 60
                    return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
                return f"{hours} hour{'s' if hours != 1 else ''} ago"
            elif days == 1:
                return "1 day ago"
            elif days < 30:
                return f"{days} days ago"
            elif days < 365:
                months = days // 30
                return f"{months} month{'s' if months != 1 else ''} ago"
            else:
                years = days // 365
                return f"{years} year{'s' if years != 1 else ''} ago"
        except Exception:
            return "unknown time ago"
