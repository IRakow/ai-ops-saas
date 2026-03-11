"""
Tenant CRUD and lifecycle management.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.crypto import encrypt_credential, generate_api_key
from app.supabase_client import get_supabase_client
from app.tenant import load_tenant, invalidate_tenant_cache
import config

logger = logging.getLogger("ai_ops.tenant_service")


def create_tenant(
    name: str,
    slug: str,
    app_name: str,
    plan: str = "trial",
    billing_email: str = "",
) -> dict:
    """Create a new tenant with workspace directory."""
    sb = get_supabase_client()
    workspace_path = str(Path(config.WORKSPACE_BASE) / slug)
    trial_ends = datetime.now(timezone.utc) + timedelta(days=config.TRIAL_DAYS)

    result = sb.table("tenants").insert({
        "name": name,
        "slug": slug,
        "plan": plan,
        "status": "trial",
        "app_name": app_name,
        "workspace_path": workspace_path,
        "billing_email": billing_email,
        "trial_ends_at": trial_ends.isoformat(),
    }).execute()

    tenant_data = result.data[0]

    # Generate initial API key
    full_key, key_hash, key_prefix = generate_api_key()
    sb.table("tenant_api_keys").insert({
        "tenant_id": tenant_data["id"],
        "name": "Default",
        "key_hash": key_hash,
        "key_prefix": key_prefix,
        "scopes": ["intake", "read", "write", "admin"],
    }).execute()

    sb.table("tenants").update({
        "api_key_hash": key_hash,
        "api_key_prefix": key_prefix,
    }).eq("id", tenant_data["id"]).execute()

    # Create workspace directory
    Path(workspace_path).mkdir(parents=True, exist_ok=True)

    logger.info(f"Created tenant {slug} (id={tenant_data['id']})")
    return {**tenant_data, "api_key": full_key}


def update_tenant(tenant_id: str, updates: dict) -> dict:
    """Update tenant settings."""
    sb = get_supabase_client()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Encrypt git credential if being updated
    if "git_credential" in updates:
        raw = updates.pop("git_credential")
        if raw:
            updates["git_credentials_encrypted"] = encrypt_credential(raw, config.SECRET_KEY)

    result = sb.table("tenants").update(updates).eq("id", tenant_id).execute()
    invalidate_tenant_cache(tenant_id)
    return result.data[0] if result.data else {}


def suspend_tenant(tenant_id: str) -> None:
    """Suspend a tenant (payment failure, policy violation, etc.)."""
    sb = get_supabase_client()
    sb.table("tenants").update({
        "status": "suspended",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", tenant_id).execute()
    invalidate_tenant_cache(tenant_id)
    logger.warning(f"Tenant {tenant_id} suspended")


def activate_tenant(tenant_id: str) -> None:
    """Activate or reactivate a tenant."""
    sb = get_supabase_client()
    sb.table("tenants").update({
        "status": "active",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", tenant_id).execute()
    invalidate_tenant_cache(tenant_id)
    logger.info(f"Tenant {tenant_id} activated")


def delete_tenant(tenant_id: str, delete_workspace: bool = True) -> None:
    """Delete a tenant and optionally their workspace."""
    tenant = load_tenant(tenant_id)
    sb = get_supabase_client()

    # Delete related data
    sb.table("tenant_api_keys").delete().eq("tenant_id", tenant_id).execute()
    sb.table("webhooks").delete().eq("tenant_id", tenant_id).execute()

    # Delete tenant
    sb.table("tenants").delete().eq("id", tenant_id).execute()
    invalidate_tenant_cache(tenant_id)

    if delete_workspace:
        from app.services.git_service import delete_workspace as del_ws
        del_ws(tenant)

    logger.info(f"Deleted tenant {tenant.slug}")


def get_tenant_usage_this_month(tenant_id: str) -> dict:
    """Get usage counts for the current billing month."""
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sb = get_supabase_client()

    fixes = sb.table("usage_records") \
        .select("id", count="exact") \
        .eq("tenant_id", tenant_id) \
        .eq("record_type", "bug_fix") \
        .gte("created_at", month_start.isoformat()) \
        .neq("status", "failed") \
        .execute()

    features = sb.table("usage_records") \
        .select("id", count="exact") \
        .eq("tenant_id", tenant_id) \
        .eq("record_type", "feature") \
        .gte("created_at", month_start.isoformat()) \
        .neq("status", "failed") \
        .execute()

    return {
        "fixes_used": fixes.count or 0,
        "features_used": features.count or 0,
    }


def check_expired_trials() -> list[str]:
    """Find and suspend expired trial tenants. Run from cron."""
    sb = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()

    result = sb.table("tenants") \
        .select("id, name") \
        .eq("status", "trial") \
        .lt("trial_ends_at", now) \
        .execute()

    suspended = []
    for tenant in result.data or []:
        suspend_tenant(tenant["id"])
        suspended.append(tenant["name"])
        logger.info(f"Trial expired for {tenant['name']}")

    return suspended
