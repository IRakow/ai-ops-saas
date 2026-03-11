"""
Public API routes — authenticated via API key.
Used by clients for programmatic access and bug intake.
"""

from flask import Blueprint, request, jsonify, g
from app.utils.api_auth import require_api_key
from app.supabase_client import get_supabase_client

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


# ── Bug Intake ───────────────────────────────────────────────────────

@api_bp.route("/intake", methods=["POST", "OPTIONS"])
@require_api_key(scopes=["intake"])
def intake():
    """Receive auto-detected or manual bug reports from client apps."""
    tenant = g.tenant
    data = request.json or {}

    sb = get_supabase_client()

    # Create session
    session_result = sb.table("ai_ops_sessions").insert({
        "tenant_id": tenant.id,
        "session_type": "bug",
        "status": "new",
        "title": (data.get("error") or "Auto-detected error")[:200],
    }).execute()
    session_id = session_result.data[0]["id"]

    # Store the bug details as the first message
    message_parts = []
    if data.get("error"):
        message_parts.append(f"**Error:** {data['error']}")
    if data.get("url"):
        message_parts.append(f"**URL:** {data['url']}")
    if data.get("user_agent"):
        message_parts.append(f"**Browser:** {data['user_agent']}")
    if data.get("metadata"):
        message_parts.append(f"**Metadata:** {data['metadata']}")

    sb.table("ai_ops_messages").insert({
        "tenant_id": tenant.id,
        "session_id": session_id,
        "role": "user",
        "content": "\n".join(message_parts) or "Auto-detected error",
    }).execute()

    # Store screenshot if provided
    if data.get("screenshot_base64"):
        sb.table("ai_ops_files").insert({
            "tenant_id": tenant.id,
            "session_id": session_id,
            "file_type": "screenshot",
            "content": data["screenshot_base64"],
        }).execute()

    # Enqueue for agent processing
    sb.table("ai_ops_agent_queue").insert({
        "tenant_id": tenant.id,
        "session_id": session_id,
        "task_type": "bug",
        "phase": "understanding",
        "description": data.get("error") or "Auto-detected error",
        "status": "pending",
    }).execute()

    return jsonify({"session_id": session_id, "status": "queued"}), 201


# ── Sessions ─────────────────────────────────────────────────────────

@api_bp.route("/sessions", methods=["GET"])
@require_api_key(scopes=["read"])
def list_sessions():
    """List sessions for this tenant."""
    tenant = g.tenant
    sb = get_supabase_client()
    result = sb.table("ai_ops_sessions") \
        .select("id, session_type, status, title, created_at") \
        .eq("tenant_id", tenant.id) \
        .order("created_at", desc=True) \
        .limit(50) \
        .execute()
    return jsonify(result.data or [])


@api_bp.route("/sessions/<session_id>", methods=["GET"])
@require_api_key(scopes=["read"])
def get_session(session_id: str):
    """Get session detail with messages."""
    tenant = g.tenant
    sb = get_supabase_client()

    session = sb.table("ai_ops_sessions") \
        .select("*") \
        .eq("id", session_id) \
        .eq("tenant_id", tenant.id) \
        .single() \
        .execute()

    messages = sb.table("ai_ops_messages") \
        .select("role, content, created_at") \
        .eq("session_id", session_id) \
        .eq("tenant_id", tenant.id) \
        .order("created_at") \
        .execute()

    return jsonify({
        "session": session.data,
        "messages": messages.data or [],
    })


@api_bp.route("/sessions/<session_id>/approve", methods=["POST"])
@require_api_key(scopes=["write"])
def approve_session(session_id: str):
    """Approve a plan for implementation."""
    tenant = g.tenant
    data = request.json or {}
    action = data.get("action", "approve")

    sb = get_supabase_client()

    if action == "approve":
        sb.table("ai_ops_sessions") \
            .update({"status": "approved"}) \
            .eq("id", session_id) \
            .eq("tenant_id", tenant.id) \
            .execute()

        sb.table("ai_ops_agent_queue").insert({
            "tenant_id": tenant.id,
            "session_id": session_id,
            "task_type": "bug",
            "phase": "execution",
            "status": "pending",
        }).execute()

    elif action == "reject":
        sb.table("ai_ops_sessions") \
            .update({"status": "rejected"}) \
            .eq("id", session_id) \
            .eq("tenant_id", tenant.id) \
            .execute()

    return jsonify({"status": action + "d"})


# ── Status ───────────────────────────────────────────────────────────

@api_bp.route("/status", methods=["GET"])
@require_api_key(scopes=["read"])
def status():
    """Get tenant status and queue depth."""
    tenant = g.tenant
    sb = get_supabase_client()

    queue = sb.table("ai_ops_agent_queue") \
        .select("id", count="exact") \
        .eq("tenant_id", tenant.id) \
        .eq("status", "pending") \
        .execute()

    active = sb.table("ai_ops_agent_queue") \
        .select("id", count="exact") \
        .eq("tenant_id", tenant.id) \
        .eq("status", "processing") \
        .execute()

    return jsonify({
        "tenant": tenant.status,
        "plan": tenant.plan,
        "queue_depth": queue.count or 0,
        "active_agents": active.count or 0,
    })


# ── Webhooks ─────────────────────────────────────────────────────────

@api_bp.route("/webhooks", methods=["GET"])
@require_api_key(scopes=["admin"])
def list_webhooks():
    tenant = g.tenant
    sb = get_supabase_client()
    result = sb.table("webhooks") \
        .select("id, url, events, is_active, last_triggered_at") \
        .eq("tenant_id", tenant.id) \
        .execute()
    return jsonify(result.data or [])


@api_bp.route("/webhooks", methods=["POST"])
@require_api_key(scopes=["admin"])
def create_webhook():
    tenant = g.tenant
    data = request.json or {}
    import secrets

    sb = get_supabase_client()
    webhook_secret = secrets.token_hex(32)

    result = sb.table("webhooks").insert({
        "tenant_id": tenant.id,
        "url": data["url"],
        "events": data.get("events", ["fix.completed"]),
        "secret": webhook_secret,
    }).execute()

    return jsonify({
        "id": result.data[0]["id"],
        "secret": webhook_secret,
    }), 201


@api_bp.route("/webhooks/<webhook_id>", methods=["DELETE"])
@require_api_key(scopes=["admin"])
def delete_webhook(webhook_id: str):
    tenant = g.tenant
    sb = get_supabase_client()
    sb.table("webhooks") \
        .delete() \
        .eq("id", webhook_id) \
        .eq("tenant_id", tenant.id) \
        .execute()
    return "", 204
