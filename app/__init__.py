from flask import Flask, session, g, request
import config


def create_app():
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config["APP_NAME"] = config.SAAS_NAME

    # ── Register blueprints ──────────────────────────────────────────
    from app.routes.ai_ops import ai_ops_bp
    from app.routes.bug_intake import bug_intake_bp
    from app.routes.admin import admin_bp
    from app.routes.api import api_bp
    from app.routes.billing import billing_bp
    from app.routes.onboarding import onboarding_bp

    app.register_blueprint(ai_ops_bp)
    app.register_blueprint(bug_intake_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(onboarding_bp)

    # ── Tenant context middleware ────────────────────────────────────
    @app.before_request
    def load_tenant_context():
        """Load tenant config into g if a tenant session is active."""
        g.tenant = None
        g.tenant_id = session.get("ai_ops_tenant_id")
        if g.tenant_id:
            try:
                from app.tenant import load_tenant
                g.tenant = load_tenant(g.tenant_id)
            except Exception:
                pass

    # ── CORS for intake endpoint ─────────────────────────────────────
    @app.after_request
    def add_cors_headers(response):
        if request.path.startswith("/api/v1/intake"):
            origin = request.headers.get("Origin", "*")
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
        return response

    # ── Template context ─────────────────────────────────────────────
    @app.context_processor
    def inject_config():
        tenant = getattr(g, "tenant", None)
        return {
            "config": config,
            "tenant": tenant,
            "saas_name": config.SAAS_NAME,
        }

    # ── Health check ─────────────────────────────────────────────────
    @app.route("/health")
    def health():
        from app.supabase_client import get_supabase_client
        try:
            sb = get_supabase_client()
            sb.table("tenants").select("id").limit(1).execute()
            db_status = "connected"
        except Exception as e:
            db_status = f"error: {e}"

        from flask import jsonify
        return jsonify({
            "status": "healthy" if db_status == "connected" else "degraded",
            "supabase": db_status,
        })

    return app
