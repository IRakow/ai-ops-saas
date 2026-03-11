"""
Usage tracking and limit enforcement.
Records every agent pipeline run for billing and analytics.
"""

import logging
from datetime import datetime, timezone

from app.supabase_client import get_supabase_client
from app.tenant import load_tenant

logger = logging.getLogger("ai_ops.usage")


def start_usage_record(tenant_id: str, session_id: str, record_type: str) -> str:
    """Create a usage record when a pipeline starts. Returns the record ID."""
    sb = get_supabase_client()
    result = sb.table("usage_records").insert({
        "tenant_id": tenant_id,
        "session_id": session_id,
        "record_type": record_type,
        "status": "started",
    }).execute()
    return result.data[0]["id"]


def complete_usage_record(
    record_id: str,
    verdict: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_seconds: int = 0,
    agents_used: list[dict] | None = None,
    retries: int = 0,
) -> None:
    """Update a usage record when the pipeline finishes."""
    # Estimate cost: $15/M input, $75/M output for Opus
    cost_cents = int((input_tokens * 15 + output_tokens * 75) / 1_000_000 * 100)

    sb = get_supabase_client()
    sb.table("usage_records").update({
        "status": "completed",
        "verdict": verdict,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost_cents": cost_cents,
        "duration_seconds": duration_seconds,
        "agents_used": agents_used or [],
        "retries": retries,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", record_id).execute()


def fail_usage_record(record_id: str, reason: str = "") -> None:
    """Mark a usage record as failed."""
    sb = get_supabase_client()
    sb.table("usage_records").update({
        "status": "failed",
        "verdict": "FAILED",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", record_id).execute()


def check_limits(tenant_id: str, task_type: str) -> tuple[bool, str]:
    """
    Check if a tenant can run another task.
    Returns (allowed, reason).
    """
    tenant = load_tenant(tenant_id)

    if tenant.status not in ("active", "trial"):
        return False, f"Tenant is {tenant.status}"

    # Enterprise = unlimited
    if tenant.plan == "enterprise":
        return True, ""

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )

    sb = get_supabase_client()
    usage = sb.table("usage_records") \
        .select("id", count="exact") \
        .eq("tenant_id", tenant_id) \
        .eq("record_type", task_type) \
        .gte("created_at", month_start.isoformat()) \
        .neq("status", "failed") \
        .execute()

    count = usage.count or 0

    if task_type == "bug_fix":
        limit = tenant.monthly_fix_limit
        if count >= limit:
            return False, f"Monthly fix limit reached ({count}/{limit})"
    elif task_type == "feature":
        limit = tenant.monthly_feature_limit
        if count >= limit:
            return False, f"Monthly feature limit reached ({count}/{limit})"

    return True, ""


def get_monthly_summary(tenant_id: str) -> dict:
    """Get usage summary for the current month."""
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )

    sb = get_supabase_client()
    records = sb.table("usage_records") \
        .select("*") \
        .eq("tenant_id", tenant_id) \
        .gte("created_at", month_start.isoformat()) \
        .execute()

    data = records.data or []

    fixes_attempted = sum(1 for r in data if r["record_type"] == "bug_fix")
    fixes_succeeded = sum(1 for r in data if r["record_type"] == "bug_fix" and r["verdict"] == "FIXED")
    features_attempted = sum(1 for r in data if r["record_type"] == "feature")
    features_succeeded = sum(1 for r in data if r["record_type"] == "feature" and r["verdict"] == "FIXED")
    total_cost_cents = sum(r.get("total_cost_cents", 0) for r in data)
    total_duration = sum(r.get("duration_seconds", 0) for r in data)
    total_input_tokens = sum(r.get("input_tokens", 0) for r in data)
    total_output_tokens = sum(r.get("output_tokens", 0) for r in data)

    return {
        "fixes_attempted": fixes_attempted,
        "fixes_succeeded": fixes_succeeded,
        "features_attempted": features_attempted,
        "features_succeeded": features_succeeded,
        "total_cost_dollars": round(total_cost_cents / 100, 2),
        "total_duration_minutes": round(total_duration / 60, 1),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }
