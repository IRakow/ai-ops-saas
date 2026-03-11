"""
Tenant user authentication.
Session-based auth scoped to a specific tenant.
"""

import functools
from flask import session, redirect, url_for, request, g
import logging

logger = logging.getLogger(__name__)


def ai_ops_auth_required(f):
    """Require authenticated tenant user with tenant context."""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("ai_ops_user_id") or not session.get("ai_ops_tenant_id"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                from flask import jsonify
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("ai_ops.login"))

        # Ensure tenant is loaded into g
        if not getattr(g, "tenant", None):
            try:
                from app.tenant import load_tenant
                g.tenant = load_tenant(session["ai_ops_tenant_id"])
                g.tenant_id = session["ai_ops_tenant_id"]
            except Exception:
                session.clear()
                return redirect(url_for("ai_ops.login"))

        return f(*args, **kwargs)
    return decorated_function


def tenant_admin_required(f):
    """Require tenant admin role."""
    @functools.wraps(f)
    @ai_ops_auth_required
    def decorated_function(*args, **kwargs):
        if session.get("ai_ops_user_role") != "admin":
            from flask import jsonify
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated_function


def get_current_ai_ops_user() -> dict:
    """Get the current tenant user from session."""
    return {
        "id": session.get("ai_ops_user_id"),
        "name": session.get("ai_ops_user_name"),
        "email": session.get("ai_ops_user_email"),
        "role": session.get("ai_ops_user_role", "user"),
        "tenant_id": session.get("ai_ops_tenant_id"),
        "tenant_slug": session.get("ai_ops_tenant_slug"),
    }
