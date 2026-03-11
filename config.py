import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── SaaS identity ────────────────────────────────────────────────────
SAAS_DOMAIN = os.getenv("SAAS_DOMAIN", "localhost:5000")
SAAS_NAME = os.getenv("SAAS_NAME", "AI Ops")

# App identity (operator-level defaults, overridden per-tenant)
APP_NAME = os.getenv("APP_NAME", "My Application")
APP_DESCRIPTION = os.getenv("APP_DESCRIPTION", "A web application")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

# Paths
WORKING_DIR = os.getenv("WORKING_DIR", str(Path.cwd()))
TOOLS_DIR = os.getenv("TOOLS_DIR", str(Path(__file__).parent / "tools"))
LOG_DIR = os.getenv("LOG_DIR", "/var/log/ai-ops")
PROTOCOL_FILE = os.getenv("PROTOCOL_FILE", "")
BLAST_RADIUS_FILE = os.getenv("BLAST_RADIUS_FILE", "blast_radius.json")
CODEBASE_CONTEXT_FILE = os.getenv("CODEBASE_CONTEXT_FILE", "codebase_context.md")
WORKSPACE_BASE = os.getenv("WORKSPACE_BASE", str(Path(__file__).parent / "workspaces"))
BACKEND_DIR = os.getenv("BACKEND_DIR", "")

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
PRODUCTION_SUPABASE_REF = os.getenv("PRODUCTION_SUPABASE_REF", "")

# Claude
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-opus-4-6")
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "1800"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

# Deploy
PRODUCTION_VM = os.getenv("PRODUCTION_VM", "")
PRODUCTION_DEPLOY_SCRIPT = os.getenv("PRODUCTION_DEPLOY_SCRIPT", "")
PRODUCTION_BASE_URL = os.getenv("PRODUCTION_BASE_URL", "")
STAGING_DEPLOY_SCRIPT = os.getenv("STAGING_DEPLOY_SCRIPT", "")
STAGING_URL = os.getenv("STAGING_URL", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Notifications
NOTIFICATION_EMAILS = [e.strip() for e in os.getenv("NOTIFICATION_EMAILS", "").split(",") if e.strip()]
NOTIFICATION_PHONE = os.getenv("NOTIFICATION_PHONE", "")
NOTIFICATION_FROM_EMAIL = os.getenv("NOTIFICATION_FROM_EMAIL", "noreply@example.com")
SYSTEM_USER_EMAIL = os.getenv("SYSTEM_USER_EMAIL", "system@ai-ops.local")

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

# SendGrid
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")

# Gemini (for notes analysis)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Flask
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
FLASK_ENV = os.getenv("FLASK_ENV", "production")

# Soak check
SOAK_CHECK_EMAIL = os.getenv("SOAK_CHECK_EMAIL", "")
SOAK_CHECK_PASSWORD = os.getenv("SOAK_CHECK_PASSWORD", "")

# Python path for subprocess
PYTHON_PATH = os.getenv("PYTHON_PATH", "python3")
GUNICORN_PATTERN = os.getenv("GUNICORN_PATTERN", "gunicorn.*wsgi")

# Validate script names (configurable per-project)
VALIDATE_SCRIPT = os.getenv("VALIDATE_SCRIPT", "")
GIT_GUARD_SCRIPT = os.getenv("GIT_GUARD_SCRIPT", "")
BROWSER_SMOKE_SCRIPT = os.getenv("BROWSER_SMOKE_SCRIPT", "browser_smoke_test.py")

# VM/Deploy details
PRODUCTION_VM_ZONE = os.getenv("PRODUCTION_VM_ZONE", "us-central1-a")
PRODUCTION_APP_DIR = os.getenv("PRODUCTION_APP_DIR", "")
PRODUCTION_SUPERVISOR_NAME = os.getenv("PRODUCTION_SUPERVISOR_NAME", "")
VM_USER = os.getenv("VM_USER", "ubuntu")
VM_HOME = os.getenv("VM_HOME", f"/home/{VM_USER}")
DEPLOY_WORKFLOW = os.getenv("DEPLOY_WORKFLOW", "deploy-production.yml")
TEST_BASE_URL = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
ERROR_LOG_PATH = os.getenv("ERROR_LOG_PATH", "")

# Notification phones (comma-separated)
NOTIFICATION_PHONES = [p.strip() for p in os.getenv("NOTIFICATION_PHONES", "").split(",") if p.strip()]

# Notification recipients (JSON array or default to NOTIFICATION_EMAILS)
NOTIFICATION_RECIPIENTS = os.getenv("NOTIFICATION_RECIPIENTS", "")

# Test VM IP (for environment detection in bug intake)
TEST_VM_IP = os.getenv("TEST_VM_IP", "")

# ── Valor Payment Systems ────────────────────────────────────────────
VALOR_API_BASE = os.getenv("VALOR_API_BASE", "https://api.valorpaytech.com/v1")
VALOR_API_KEY = os.getenv("VALOR_API_KEY", "")
VALOR_APP_ID = os.getenv("VALOR_APP_ID", "")
VALOR_WEBHOOK_SECRET = os.getenv("VALOR_WEBHOOK_SECRET", "")

# ── SaaS settings ────────────────────────────────────────────────────
MAX_TENANTS = int(os.getenv("MAX_TENANTS", "50"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
DEFAULT_PLAN = os.getenv("DEFAULT_PLAN", "trial")


def get_codebase_context() -> str:
    """Load codebase context from file for agent prompts (operator-level fallback)."""
    path = Path(CODEBASE_CONTEXT_FILE)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    if path.exists():
        return path.read_text().strip()
    return f"{APP_NAME} — {APP_DESCRIPTION}"
