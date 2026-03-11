"""
API key authentication middleware.
Validates X-API-Key header and loads tenant context.
"""

import hashlib
from functools import wraps
from flask import request, jsonify, g


def require_api_key(scopes: list[str] | None = None):
    """Decorator that validates API key and sets g.tenant."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
            if not api_key:
                return jsonify({"error": "Missing API key"}), 401

            key_hash = hashlib.sha256(api_key.encode()).hexdigest()

            from app.tenant import load_tenant_by_api_key_hash
            tenant = load_tenant_by_api_key_hash(key_hash)
            if not tenant:
                return jsonify({"error": "Invalid API key"}), 401

            if tenant.status not in ("active", "trial"):
                return jsonify({"error": "Tenant suspended"}), 403

            # Check scopes if specified
            if scopes:
                from app.supabase_client import get_supabase_client
                sb = get_supabase_client()
                key_row = sb.table("tenant_api_keys") \
                    .select("scopes") \
                    .eq("key_hash", key_hash) \
                    .single() \
                    .execute()
                key_scopes = key_row.data.get("scopes", [])
                if not any(s in key_scopes for s in scopes):
                    return jsonify({"error": "Insufficient scope"}), 403

            g.tenant = tenant
            g.tenant_id = tenant.id
            return f(*args, **kwargs)
        return decorated
    return decorator
