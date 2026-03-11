"""
Onboarding wizard routes — walks new tenants through setup.
5 steps: welcome → connect repo → scan codebase → configure delivery → setup detection
"""

import json
import logging
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
from flask import (
    Blueprint, render_template, request, session,
    redirect, url_for, flash, jsonify,
)

from app.supabase_client import get_supabase_client
from app.crypto import encrypt_credential, generate_api_key
import config

onboarding_bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")
logger = logging.getLogger("ai_ops.onboarding")


@onboarding_bp.route("/", methods=["GET"])
def start():
    """Landing — redirect to step 1 or dashboard if already onboarded."""
    if session.get("ai_ops_tenant_id"):
        from app.tenant import load_tenant
        tenant = load_tenant(session["ai_ops_tenant_id"])
        if tenant.onboarded_at:
            return redirect(url_for("ai_ops.dashboard"))
    return redirect(url_for("onboarding.welcome"))


# ── Step 1: Welcome + Account Creation ───────────────────────────────

@onboarding_bp.route("/welcome", methods=["GET", "POST"])
def welcome():
    if request.method == "GET":
        return render_template("onboarding/welcome.html")

    # Create tenant + admin user
    name = request.form.get("company_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    app_name = request.form.get("app_name", "").strip()
    plan = request.form.get("plan", "trial")

    if not all([name, email, password, app_name]):
        flash("All fields are required", "error")
        return render_template("onboarding/welcome.html"), 400

    # Generate slug from company name
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")[:50]

    sb = get_supabase_client()

    # Check slug uniqueness
    existing = sb.table("tenants").select("id").eq("slug", slug).maybe_single().execute()
    if existing.data:
        slug = f"{slug}-{secrets.token_hex(3)}"

    # Create tenant
    trial_ends = datetime.now(timezone.utc) + timedelta(days=config.TRIAL_DAYS)
    workspace_path = str(Path(config.WORKSPACE_BASE) / slug)

    tenant_result = sb.table("tenants").insert({
        "name": name,
        "slug": slug,
        "plan": plan,
        "status": "trial",
        "app_name": app_name,
        "workspace_path": workspace_path,
        "trial_ends_at": trial_ends.isoformat(),
    }).execute()
    tenant_id = tenant_result.data[0]["id"]

    # Create admin user for this tenant
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_result = sb.table("ai_ops_users").insert({
        "tenant_id": tenant_id,
        "name": name,
        "email": email,
        "password_hash": pw_hash,
        "role": "admin",
    }).execute()

    # Generate initial API key
    full_key, key_hash, key_prefix = generate_api_key()
    sb.table("tenant_api_keys").insert({
        "tenant_id": tenant_id,
        "name": "Default",
        "key_hash": key_hash,
        "key_prefix": key_prefix,
        "scopes": ["intake", "read", "write", "admin"],
    }).execute()

    # Store API key prefix on tenant for quick lookup
    sb.table("tenants").update({
        "api_key_hash": key_hash,
        "api_key_prefix": key_prefix,
    }).eq("id", tenant_id).execute()

    # Log user in
    session["ai_ops_user_id"] = user_result.data[0]["id"]
    session["ai_ops_user_name"] = name
    session["ai_ops_user_email"] = email
    session["ai_ops_user_role"] = "admin"
    session["ai_ops_tenant_id"] = tenant_id
    session["ai_ops_tenant_slug"] = slug
    session["onboarding_api_key"] = full_key

    return redirect(url_for("onboarding.connect_repo"))


# ── Step 2: Connect Repository ───────────────────────────────────────

@onboarding_bp.route("/connect-repo", methods=["GET", "POST"])
def connect_repo():
    tenant_id = session.get("ai_ops_tenant_id")
    if not tenant_id:
        return redirect(url_for("onboarding.welcome"))

    if request.method == "GET":
        return render_template("onboarding/connect_repo.html")

    repo_url = request.form.get("repo_url", "").strip()
    git_provider = request.form.get("git_provider", "github")
    credential = request.form.get("credential", "").strip()
    default_branch = request.form.get("default_branch", "main").strip()

    if not repo_url:
        flash("Repository URL is required", "error")
        return render_template("onboarding/connect_repo.html"), 400

    sb = get_supabase_client()

    # Encrypt the credential
    encrypted = encrypt_credential(credential, config.SECRET_KEY) if credential else ""

    sb.table("tenants").update({
        "git_repo_url": repo_url,
        "git_provider": git_provider,
        "git_credentials_encrypted": encrypted,
        "git_default_branch": default_branch,
        "git_deploy_branch": default_branch,
    }).eq("id", tenant_id).execute()

    # Attempt clone
    from app.tenant import load_tenant, invalidate_tenant_cache
    invalidate_tenant_cache(tenant_id)
    tenant = load_tenant(tenant_id)

    try:
        from app.services.git_service import clone_workspace
        clone_workspace(tenant)
        flash("Repository cloned successfully", "success")
    except Exception as e:
        logger.error(f"Clone failed for {tenant_id}: {e}")
        flash(f"Clone failed: {e}. Check your credentials and try again.", "error")
        return render_template("onboarding/connect_repo.html"), 400

    return redirect(url_for("onboarding.scan_codebase"))


# ── Step 3: Scan Codebase ────────────────────────────────────────────

@onboarding_bp.route("/scan-codebase", methods=["GET", "POST"])
def scan_codebase():
    tenant_id = session.get("ai_ops_tenant_id")
    if not tenant_id:
        return redirect(url_for("onboarding.welcome"))

    from app.tenant import load_tenant
    tenant = load_tenant(tenant_id)

    if request.method == "GET":
        # Auto-scan on first visit
        context = ""
        if tenant.workspace_path and Path(tenant.workspace_path).is_dir():
            try:
                from generate_context import scan_and_generate
                context = scan_and_generate(
                    tenant.workspace_path,
                    app_name=tenant.app_name,
                    app_description=tenant.app_description or "A web application",
                    app_url=tenant.app_url or "",
                )
            except Exception as e:
                logger.error(f"Context scan failed: {e}")
                context = f"# {tenant.app_name}\n\nAuto-scan failed. Please describe your codebase manually."

        return render_template("onboarding/scan_codebase.html",
            context=context,
            tenant=tenant,
        )

    # Save edited context
    context = request.form.get("codebase_context", "").strip()
    sb = get_supabase_client()
    sb.table("tenants").update({
        "codebase_context": context,
    }).eq("id", tenant_id).execute()

    # Generate manifest
    if tenant.workspace_path and Path(tenant.workspace_path).is_dir():
        try:
            from manifest_generator import generate_manifest
            manifest = generate_manifest(tenant.workspace_path)
            sb.table("tenants").update({
                "manifest": manifest,
            }).eq("id", tenant_id).execute()
        except Exception as e:
            logger.warning(f"Manifest generation failed: {e}")

    from app.tenant import invalidate_tenant_cache
    invalidate_tenant_cache(tenant_id)

    return redirect(url_for("onboarding.configure_delivery"))


# ── Step 4: Configure Delivery ───────────────────────────────────────

@onboarding_bp.route("/configure-delivery", methods=["GET", "POST"])
def configure_delivery():
    tenant_id = session.get("ai_ops_tenant_id")
    if not tenant_id:
        return redirect(url_for("onboarding.welcome"))

    if request.method == "GET":
        return render_template("onboarding/configure_delivery.html")

    deploy_method = request.form.get("deploy_method", "github_pr")
    deploy_branch = request.form.get("deploy_branch", "main").strip()
    notification_emails = request.form.get("notification_emails", "").strip()

    sb = get_supabase_client()
    emails = [e.strip() for e in notification_emails.split(",") if e.strip()]

    sb.table("tenants").update({
        "deploy_method": deploy_method,
        "git_deploy_branch": deploy_branch,
        "notification_emails": emails,
    }).eq("id", tenant_id).execute()

    from app.tenant import invalidate_tenant_cache
    invalidate_tenant_cache(tenant_id)

    return redirect(url_for("onboarding.setup_detection"))


# ── Step 5: Setup Bug Detection ──────────────────────────────────────

@onboarding_bp.route("/setup-detection", methods=["GET", "POST"])
def setup_detection():
    tenant_id = session.get("ai_ops_tenant_id")
    if not tenant_id:
        return redirect(url_for("onboarding.welcome"))

    api_key = session.get("onboarding_api_key", "YOUR_API_KEY")
    intake_url = f"https://{config.SAAS_DOMAIN}/api/v1/intake"

    if request.method == "GET":
        return render_template("onboarding/setup_detection.html",
            api_key=api_key,
            intake_url=intake_url,
            saas_domain=config.SAAS_DOMAIN,
        )

    # Mark onboarding complete
    sb = get_supabase_client()
    sb.table("tenants").update({
        "onboarded_at": datetime.now(timezone.utc).isoformat(),
        "status": "trial",
    }).eq("id", tenant_id).execute()

    from app.tenant import invalidate_tenant_cache
    invalidate_tenant_cache(tenant_id)
    session.pop("onboarding_api_key", None)

    flash("Setup complete! You're ready to start debugging.", "success")
    return redirect(url_for("ai_ops.dashboard"))
