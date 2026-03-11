"""
Tenant configuration model and loader.
Loads per-tenant settings from Supabase for multi-tenant agent runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.supabase_client import get_supabase_client

logger = logging.getLogger("ai_ops.tenant")

_tenant_cache: dict[str, tuple[TenantConfig, float]] = {}
CACHE_TTL_SECONDS = 60


@dataclass
class TenantConfig:
    """All settings for a single tenant, used throughout the pipeline."""

    # Identity
    id: str
    name: str
    slug: str
    plan: str
    status: str

    # Git
    git_repo_url: str = ""
    git_provider: str = "github"
    git_credentials_encrypted: str = ""
    git_default_branch: str = "main"
    git_deploy_branch: str = "main"

    # Workspace
    workspace_path: str = ""
    last_git_sync: str | None = None

    # Codebase context
    codebase_context: str = ""
    blast_radius: dict = field(default_factory=dict)
    agent_protocol: str = ""
    manifest: dict = field(default_factory=dict)

    # App info
    app_name: str = ""
    app_description: str = ""
    app_url: str = ""
    app_stack: str = ""

    # Deploy
    deploy_method: str = "github_pr"
    deploy_config: dict = field(default_factory=dict)

    # Notifications
    notification_emails: list[str] = field(default_factory=list)
    notification_webhook_url: str = ""
    notification_slack_webhook: str = ""

    # Billing
    valor_customer_id: str = ""
    valor_subscription_id: str = ""
    billing_email: str = ""
    monthly_fix_limit: int = 10
    monthly_feature_limit: int = 2

    # API
    api_key_hash: str = ""
    api_key_prefix: str = ""

    @property
    def working_dir(self) -> str:
        return self.workspace_path

    def get_context(self) -> str:
        if self.codebase_context:
            return self.codebase_context
        return f"{self.app_name} — {self.app_description}"

    def get_blast_radius_for_module(self, module: str) -> list[str] | None:
        if not self.blast_radius:
            return None
        return self.blast_radius.get(module)


def load_tenant(tenant_id: str) -> TenantConfig:
    """Load tenant config from Supabase, with short cache."""
    import time

    now = time.time()
    if tenant_id in _tenant_cache:
        cached, cached_at = _tenant_cache[tenant_id]
        if now - cached_at < CACHE_TTL_SECONDS:
            return cached

    sb = get_supabase_client()
    result = sb.table("tenants").select("*").eq("id", tenant_id).single().execute()
    row = result.data

    tenant = TenantConfig(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        plan=row["plan"],
        status=row["status"],
        git_repo_url=row.get("git_repo_url") or "",
        git_provider=row.get("git_provider") or "github",
        git_credentials_encrypted=row.get("git_credentials_encrypted") or "",
        git_default_branch=row.get("git_default_branch") or "main",
        git_deploy_branch=row.get("git_deploy_branch") or "main",
        workspace_path=row.get("workspace_path") or "",
        last_git_sync=row.get("last_git_sync"),
        codebase_context=row.get("codebase_context") or "",
        blast_radius=row.get("blast_radius") or {},
        agent_protocol=row.get("agent_protocol") or "",
        manifest=row.get("manifest") or {},
        app_name=row.get("app_name") or row["name"],
        app_description=row.get("app_description") or "",
        app_url=row.get("app_url") or "",
        app_stack=row.get("app_stack") or "",
        deploy_method=row.get("deploy_method") or "github_pr",
        deploy_config=row.get("deploy_config") or {},
        notification_emails=row.get("notification_emails") or [],
        notification_webhook_url=row.get("notification_webhook_url") or "",
        notification_slack_webhook=row.get("notification_slack_webhook") or "",
        valor_customer_id=row.get("valor_customer_id") or "",
        valor_subscription_id=row.get("valor_subscription_id") or "",
        billing_email=row.get("billing_email") or "",
        monthly_fix_limit=row.get("monthly_fix_limit") or 10,
        monthly_feature_limit=row.get("monthly_feature_limit") or 2,
        api_key_hash=row.get("api_key_hash") or "",
        api_key_prefix=row.get("api_key_prefix") or "",
    )

    _tenant_cache[tenant_id] = (tenant, now)
    return tenant


def load_tenant_by_slug(slug: str) -> TenantConfig | None:
    """Load tenant by URL slug."""
    sb = get_supabase_client()
    result = sb.table("tenants").select("id").eq("slug", slug).maybe_single().execute()
    if not result.data:
        return None
    return load_tenant(result.data["id"])


def load_tenant_by_api_key_hash(key_hash: str) -> TenantConfig | None:
    """Look up tenant from a hashed API key."""
    sb = get_supabase_client()
    result = sb.table("tenant_api_keys") \
        .select("tenant_id") \
        .eq("key_hash", key_hash) \
        .eq("is_active", True) \
        .maybe_single() \
        .execute()
    if not result.data:
        return None

    # Update last_used_at
    sb.table("tenant_api_keys") \
        .update({"last_used_at": datetime.utcnow().isoformat()}) \
        .eq("key_hash", key_hash) \
        .execute()

    return load_tenant(result.data["tenant_id"])


def invalidate_tenant_cache(tenant_id: str) -> None:
    """Clear cached tenant config after an update."""
    _tenant_cache.pop(tenant_id, None)


def list_active_tenants() -> list[dict[str, Any]]:
    """List all active tenants (for admin dashboard)."""
    sb = get_supabase_client()
    result = sb.table("tenants") \
        .select("id, name, slug, plan, status, app_name, created_at") \
        .in_("status", ["active", "trial"]) \
        .order("created_at") \
        .execute()
    return result.data or []
