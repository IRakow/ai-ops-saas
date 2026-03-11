"""
Operator admin routes — manages tenants, billing, system health.
"""

import bcrypt
from flask import (
    Blueprint, render_template, request, session,
    redirect, url_for, flash, jsonify,
)

from app.supabase_client import get_supabase_client
from app.utils.admin_auth import operator_admin_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ── Auth ─────────────────────────────────────────────────────────────

@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("admin/login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    sb = get_supabase_client()
    result = sb.table("operator_admins") \
        .select("*") \
        .eq("email", email) \
        .maybe_single() \
        .execute()

    if not result.data:
        flash("Invalid credentials", "error")
        return render_template("admin/login.html"), 401

    admin = result.data
    if not bcrypt.checkpw(password.encode(), admin["password_hash"].encode()):
        flash("Invalid credentials", "error")
        return render_template("admin/login.html"), 401

    session["operator_admin"] = True
    session["operator_admin_id"] = admin["id"]
    session["operator_admin_name"] = admin["name"]

    sb.table("operator_admins") \
        .update({"last_login_at": "now()"}) \
        .eq("id", admin["id"]) \
        .execute()

    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/logout")
def logout():
    session.pop("operator_admin", None)
    session.pop("operator_admin_id", None)
    session.pop("operator_admin_name", None)
    return redirect(url_for("admin.login"))


# ── Dashboard ────────────────────────────────────────────────────────

@admin_bp.route("/")
@operator_admin_required
def dashboard():
    sb = get_supabase_client()

    tenants = sb.table("tenants").select("id, status, plan").execute()
    tenant_data = tenants.data or []

    total = len(tenant_data)
    active = sum(1 for t in tenant_data if t["status"] == "active")
    trial = sum(1 for t in tenant_data if t["status"] == "trial")
    suspended = sum(1 for t in tenant_data if t["status"] == "suspended")

    queue = sb.table("ai_ops_agent_queue") \
        .select("id", count="exact") \
        .eq("status", "pending") \
        .execute()
    queue_depth = queue.count or 0

    return render_template("admin/dashboard.html",
        total_tenants=total,
        active_tenants=active,
        trial_tenants=trial,
        suspended_tenants=suspended,
        queue_depth=queue_depth,
    )


# ── Tenants ──────────────────────────────────────────────────────────

@admin_bp.route("/tenants")
@operator_admin_required
def tenants():
    sb = get_supabase_client()
    result = sb.table("tenants") \
        .select("*") \
        .order("created_at", desc=True) \
        .execute()
    return render_template("admin/tenants.html", tenants=result.data or [])


@admin_bp.route("/tenants/<tenant_id>")
@operator_admin_required
def tenant_detail(tenant_id: str):
    sb = get_supabase_client()
    tenant = sb.table("tenants").select("*").eq("id", tenant_id).single().execute()

    sessions = sb.table("ai_ops_sessions") \
        .select("id, session_type, status, created_at") \
        .eq("tenant_id", tenant_id) \
        .order("created_at", desc=True) \
        .limit(20) \
        .execute()

    usage = sb.table("usage_records") \
        .select("*") \
        .eq("tenant_id", tenant_id) \
        .order("created_at", desc=True) \
        .limit(50) \
        .execute()

    return render_template("admin/tenant_detail.html",
        tenant=tenant.data,
        sessions=sessions.data or [],
        usage=usage.data or [],
    )


@admin_bp.route("/tenants/<tenant_id>/suspend", methods=["POST"])
@operator_admin_required
def suspend_tenant(tenant_id: str):
    sb = get_supabase_client()
    sb.table("tenants").update({"status": "suspended"}).eq("id", tenant_id).execute()
    flash("Tenant suspended", "warning")
    return redirect(url_for("admin.tenant_detail", tenant_id=tenant_id))


@admin_bp.route("/tenants/<tenant_id>/activate", methods=["POST"])
@operator_admin_required
def activate_tenant(tenant_id: str):
    sb = get_supabase_client()
    sb.table("tenants").update({"status": "active"}).eq("id", tenant_id).execute()
    flash("Tenant activated", "success")
    return redirect(url_for("admin.tenant_detail", tenant_id=tenant_id))


# ── Queue ────────────────────────────────────────────────────────────

@admin_bp.route("/queue")
@operator_admin_required
def queue():
    sb = get_supabase_client()
    tasks = sb.table("ai_ops_agent_queue") \
        .select("*, tenants(name, slug)") \
        .in_("status", ["pending", "processing"]) \
        .order("created_at") \
        .execute()
    return render_template("admin/queue.html", tasks=tasks.data or [])


# ── System ───────────────────────────────────────────────────────────

@admin_bp.route("/system")
@operator_admin_required
def system():
    import shutil
    import config as cfg
    from pathlib import Path

    workspace_dir = Path(cfg.WORKSPACE_BASE)
    disk = shutil.disk_usage(str(workspace_dir)) if workspace_dir.exists() else None

    return render_template("admin/system.html",
        workspace_base=cfg.WORKSPACE_BASE,
        agent_model=cfg.AGENT_MODEL,
        disk_total_gb=round(disk.total / (1024**3), 1) if disk else 0,
        disk_used_gb=round(disk.used / (1024**3), 1) if disk else 0,
        disk_free_gb=round(disk.free / (1024**3), 1) if disk else 0,
    )


# ── Admin API ────────────────────────────────────────────────────────

@admin_bp.route("/api/tenants", methods=["GET"])
@operator_admin_required
def api_tenants():
    sb = get_supabase_client()
    result = sb.table("tenants") \
        .select("id, name, slug, plan, status, app_name, created_at") \
        .execute()
    return jsonify(result.data or [])


@admin_bp.route("/api/system/health")
@operator_admin_required
def api_system_health():
    sb = get_supabase_client()
    try:
        sb.table("tenants").select("id").limit(1).execute()
        db = "connected"
    except Exception as e:
        db = f"error: {e}"

    return jsonify({"supabase": db, "status": "ok" if db == "connected" else "degraded"})
