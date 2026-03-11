# AI Ops SaaS

Multi-agent AI debugging and feature delivery as a service. Your clients paste a JavaScript snippet into their app, connect their GitHub repo, and watch as teams of Claude Opus agents automatically detect, investigate, fix, and deploy bug fixes and feature requests.

You (the operator) run one server that serves multiple clients. Each client gets their own isolated workspace, agent pipeline, and billing. They never see the agent code or prompts.

## How It Works

```
CLIENT'S APP                    YOUR SERVER                      CLIENT'S REPO

  Browser JS  ─── bug report ──▶  Flask API
  (auto-detect                    │
   500 errors,                    ▼
   JS errors,                  Supabase
   screenshots)                   │
                                  ▼
                               Worker picks up task
                                  │
                                  ▼
                               git pull (tenant workspace)
                                  │
                                  ▼
                            ┌─────────────────────────┐
                            │  5 Specialist Agents     │
                            │  (Error Analyst,         │
                            │   Code Archaeologist,    │
                            │   DB Inspector,          │
                            │   UX Flow Mapper,        │
                            │   Dependency Auditor)    │
                            └──────────┬──────────────┘
                                       ▼
                            ┌──────────────────────┐
                            │  Consolidator Agent  │
                            └──────────┬───────────┘
                                       ▼
                            ┌──────────────────────┐
                            │  Implementer Agent   │
                            │  (writes the fix)    │
                            └──────────┬───────────┘
                                       ▼
                            ┌──────────────────────┐
                            │  Tester + Validator  │
                            │  (parallel verify)   │
                            └──────────┬───────────┘
                                       ▼
                            ┌──────────────────────┐
                            │  Assessor Agent      │
                            │  (verdict: FIXED /   │
                            │   PARTIAL / FAILED)  │
                            └──────────┬───────────┘
                                       │
                                       ▼
                                  git commit + push ──────────▶  Pull Request
                                       │
                                       ▼
                                  Notify client
                                  (email / Slack / webhook)
```

## For Operators (Quick Start)

```bash
# 1. Clone and install
git clone https://github.com/IRakow/ai-ops-saas.git /srv/ai-ops-saas/current
cd /srv/ai-ops-saas/current
python3.12 -m venv /srv/ai-ops-saas/venv
source /srv/ai-ops-saas/venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your Supabase, Valor, SendGrid, and Claude settings

# 3. Run migrations
python scripts/migrate.py

# 4. Create your admin account
python scripts/create_operator_admin.py \
    --name "Your Name" \
    --email "you@yourdomain.com" \
    --password "your-secure-password" \
    --super-admin

# 5. Start the web server
gunicorn "app:create_app()" -b 127.0.0.1:8000 -w 4

# 6. Start the worker (separate terminal or supervisor)
python worker.py
```

See [SETUP_GUIDE.md](SETUP_GUIDE.md) for the full 11-step installation with nginx, supervisor, HTTPS, and Valor billing setup.

## For Clients

Your clients do three things:

1. **Sign up** at `https://ops.yourdomain.com/signup` and connect their GitHub repo
2. **Paste this into their HTML:**

```html
<link rel="stylesheet" href="https://ops.yourdomain.com/static/css/bug-intake.css">
<script>
  window.AI_OPS_CONFIG = {
    endpoint: "https://ops.yourdomain.com/api/v1/intake",
    apiKey: "aops_live_..."
  };
</script>
<script src="https://ops.yourdomain.com/static/js/bug-intake.js"></script>
```

3. **Watch bugs get fixed** in their dashboard at `https://ops.yourdomain.com/`

See [CLIENT_GUIDE.md](CLIENT_GUIDE.md) for the full client-facing documentation.

## Requirements

- **Python 3.12+**
- **Node.js 18+** (for Claude Code CLI)
- **Claude Code CLI** installed and authenticated on the worker machine
- **Supabase** project (Pro plan for production)
- **GCP VMs** (or any Linux servers): e2-standard-2 for web, e2-standard-4 for worker
- **Valor Payment Systems** account (optional, for billing)
- **SendGrid** account (for transactional email)
- **Domain name** with DNS pointed at your server

## Pricing (Default)

| Plan | Monthly | Included | Overage |
|------|---------|----------|---------|
| Starter | $299/mo | 10 bug fixes, 2 features | $25/fix, $50/feature |
| Pro | $799/mo | 30 bug fixes, 10 features | $20/fix, $40/feature |
| Enterprise | $1,999/mo | Unlimited | -- |

Plans and pricing are fully configurable. Billing is handled via Valor Payment Systems.

## Project Structure

```
ai-ops-saas/
├── config.py                    # Operator-level settings (from .env)
├── worker.py                    # Background worker daemon
├── app/
│   ├── __init__.py              # Flask app factory
│   ├── tenant.py                # Tenant model + config loader
│   ├── crypto.py                # Fernet encryption for credentials
│   ├── routes/
│   │   ├── admin.py             # Operator admin dashboard
│   │   ├── api.py               # Public REST API
│   │   ├── billing.py           # Valor webhook handler
│   │   └── onboarding.py        # Client onboarding wizard
│   └── services/
│       ├── git_service.py       # Clone, pull, push, create PRs
│       ├── tenant_service.py    # Tenant CRUD
│       ├── billing_service.py   # Valor integration
│       ├── usage_service.py     # Usage tracking + limit enforcement
│       └── webhook_service.py   # Outgoing webhook delivery (HMAC signed)
├── migrations/                  # SQL migrations (001-011)
├── scripts/                     # Admin utilities
├── docs/                        # Detailed documentation
│   ├── architecture.md
│   ├── api_reference.md
│   ├── billing.md
│   └── security.md
└── examples/                    # Config examples (nginx, supervisor, etc.)
```

## Documentation

- [SETUP_GUIDE.md](SETUP_GUIDE.md) -- Full operator installation manual
- [CLIENT_GUIDE.md](CLIENT_GUIDE.md) -- What to give your clients
- [docs/api_reference.md](docs/api_reference.md) -- Complete API documentation
- [docs/architecture.md](docs/architecture.md) -- System architecture deep dive
- [docs/billing.md](docs/billing.md) -- Billing and usage tracking
- [docs/security.md](docs/security.md) -- Security model and tenant isolation

## License

Proprietary. All rights reserved.
