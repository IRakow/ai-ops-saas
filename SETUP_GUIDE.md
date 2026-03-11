# Setup Guide -- Operator Installation

Full installation manual for running your own AI Ops SaaS instance. This guide covers going from zero to a production-ready multi-tenant deployment.

## Prerequisites

Before you start, you need:

- **2 Linux VMs** (GCP recommended)
  - Web VM: e2-standard-2 (2 vCPU, 8 GB RAM) -- serves the Flask app
  - Worker VM: e2-standard-4 (4 vCPU, 16 GB RAM) -- runs Claude agents
  - Both running Ubuntu 22.04 LTS
- **Python 3.12+** installed on both VMs
- **Node.js 18+** installed on the worker VM (required by Claude Code CLI)
- **Claude Code CLI** installed and authenticated on the worker VM (`claude` command must work)
- **Supabase project** (Pro plan recommended for production)
- **Domain name** pointed at your web VM (e.g., `ops.yourdomain.com`)
- **Valor Payment Systems account** (optional -- only needed for automated billing)
- **SendGrid account** (for transactional email notifications)
- Optional: Twilio (for SMS alerts), Cloudflare (CDN/WAF)

---

## Step 1: Clone and Install

Run this on **both VMs**:

```bash
# Create the installation directory
sudo mkdir -p /srv/ai-ops-saas
sudo chown $USER:$USER /srv/ai-ops-saas

# Clone the repo
git clone https://github.com/IRakow/ai-ops-saas.git /srv/ai-ops-saas/current
cd /srv/ai-ops-saas/current

# Create virtual environment
python3.12 -m venv /srv/ai-ops-saas/venv
source /srv/ai-ops-saas/venv/bin/activate
pip install -r requirements.txt
```

On the **worker VM only**, create the workspaces directory:

```bash
sudo mkdir -p /srv/ai-ops-saas/workspaces
sudo chown $USER:$USER /srv/ai-ops-saas/workspaces
```

---

## Step 2: Configure Environment

```bash
cd /srv/ai-ops-saas/current
cp .env.example .env
```

Edit `.env` with your actual values. Here is every variable with explanation:

```env
# ── SaaS Identity ────────────────────────────────────────────────────
SAAS_DOMAIN=ops.yourdomain.com        # Your public domain
SAAS_NAME=AI Ops                      # Brand name shown in UI

# ── Supabase ─────────────────────────────────────────────────────────
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...      # Service role key (full access)

# ── Flask ────────────────────────────────────────────────────────────
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
FLASK_ENV=production

# ── Claude ───────────────────────────────────────────────────────────
AGENT_MODEL=claude-opus-4-6           # Model for all agents
AGENT_TIMEOUT=1800                    # Max seconds per agent subprocess
POLL_INTERVAL=10                      # Worker polls queue every N seconds

# ── Paths (worker VM) ───────────────────────────────────────────────
WORKSPACE_BASE=/srv/ai-ops-saas/workspaces
LOG_DIR=/var/log/ai-ops
TOOLS_DIR=/srv/ai-ops-saas/current/tools

# ── Valor Payment Systems (optional) ────────────────────────────────
VALOR_API_BASE=https://api.valorpaytech.com/v1
VALOR_API_KEY=                        # Your Valor API key
VALOR_APP_ID=                         # Your Valor App ID
VALOR_WEBHOOK_SECRET=                 # From Valor merchant portal

# ── SendGrid ────────────────────────────────────────────────────────
SENDGRID_API_KEY=SG...                # SendGrid API key
NOTIFICATION_FROM_EMAIL=ops@yourdomain.com

# ── Twilio (optional, for SMS alerts) ───────────────────────────────
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=

# ── Gemini (optional, for notes analysis) ───────────────────────────
GEMINI_API_KEY=

# ── SaaS Settings ───────────────────────────────────────────────────
MAX_TENANTS=50                        # Max tenants this instance supports
TRIAL_DAYS=14                         # Free trial length
DEFAULT_PLAN=trial                    # Plan assigned to new signups
```

Copy the same `.env` to both VMs.

---

## Step 3: Run Migrations

Migrations create all database tables in Supabase. You have two options:

**Option A: Using the migration script**

```bash
cd /srv/ai-ops-saas/current
source /srv/ai-ops-saas/venv/bin/activate
python scripts/migrate.py
```

If any migrations fail via the script (Supabase RPC limitations), use Option B for those.

**Option B: Run SQL directly in Supabase SQL Editor**

Go to your Supabase project dashboard, open the SQL Editor, and paste the contents of each file in order:

```
migrations/001_core_tables.sql
migrations/002_agent_queue.sql
migrations/003_notes_tables.sql
migrations/004_fix_patterns.sql
migrations/005_tenants.sql
migrations/006_operator_admins.sql
migrations/007_add_tenant_id.sql
migrations/008_api_keys.sql
migrations/009_usage_records.sql
migrations/010_webhooks.sql
migrations/011_audit_log.sql
```

Run them one at a time, in order. Each migration is idempotent.

---

## Step 4: Create Operator Admin

This creates your admin login for the operator dashboard:

```bash
cd /srv/ai-ops-saas/current
source /srv/ai-ops-saas/venv/bin/activate

python scripts/create_operator_admin.py \
    --name "Your Name" \
    --email "you@yourdomain.com" \
    --password "your-secure-password" \
    --super-admin
```

You can create additional admins later without the `--super-admin` flag.

---

## Step 5: Set Up Valor Products (Optional)

Skip this if you are not using automated billing.

```bash
python scripts/setup_valor_products.py
```

This creates three subscription products in Valor:
- Starter ($299/mo, 10 fixes + 2 features)
- Pro ($799/mo, 30 fixes + 10 features)
- Enterprise ($1,999/mo, unlimited)

The script outputs product IDs. You do not need to save these -- they are recorded in Valor's system and matched by plan name.

---

## Step 6: Configure Nginx (Web VM)

Install nginx if not already present:

```bash
sudo apt install nginx
```

Create the config:

```bash
sudo nano /etc/nginx/sites-available/ai-ops
```

See [examples/nginx.example.conf](examples/nginx.example.conf) for the full config. The key parts:

```nginx
server {
    listen 80;
    server_name ops.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location /static/ {
        alias /srv/ai-ops-saas/current/app/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
```

Enable and restart:

```bash
sudo ln -s /etc/nginx/sites-available/ai-ops /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## Step 7: Configure Supervisor

Install supervisor:

```bash
sudo apt install supervisor
```

Create log directory:

```bash
sudo mkdir -p /var/log/ai-ops
sudo chown $USER:$USER /var/log/ai-ops
```

**Web VM** -- create `/etc/supervisor/conf.d/ai-ops-web.conf`:

```ini
[program:ai-ops-web]
command=/srv/ai-ops-saas/venv/bin/gunicorn "app:create_app()" -b 127.0.0.1:8000 -w 4 --timeout 120
directory=/srv/ai-ops-saas/current
user=www-data
autostart=true
autorestart=true
startsecs=5
startretries=3
stopwaitsecs=10
environment=PATH="/srv/ai-ops-saas/venv/bin:%(ENV_PATH)s"
stdout_logfile=/var/log/ai-ops/web.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
stderr_logfile=/var/log/ai-ops/web-error.log
stderr_logfile_maxbytes=10MB
stderr_logfile_backups=3
```

**Worker VM** -- create `/etc/supervisor/conf.d/ai-ops-worker.conf`:

```ini
[program:ai-ops-worker]
command=/srv/ai-ops-saas/venv/bin/python worker.py
directory=/srv/ai-ops-saas/current
user=ai-ops
autostart=true
autorestart=true
startsecs=5
startretries=3
stopwaitsecs=30
environment=
    PATH="/srv/ai-ops-saas/venv/bin:/usr/local/bin:/usr/bin:/bin",
    HOME="/home/ai-ops",
    CI="true",
    TERM="dumb"
stdout_logfile=/var/log/ai-ops/worker.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
stderr_logfile=/var/log/ai-ops/worker-error.log
stderr_logfile_maxbytes=10MB
stderr_logfile_backups=3
```

The `CI=true` and `TERM=dumb` environment variables are required -- they prevent the Claude Code CLI from hanging on interactive prompts.

---

## Step 8: Start Services

**Web VM:**

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start ai-ops-web
```

**Worker VM:**

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start ai-ops-worker
```

Check they are running:

```bash
sudo supervisorctl status
```

---

## Step 9: Set Up HTTPS

You have two options:

**Option A: GCP HTTPS Load Balancer (recommended for GCP VMs)**

1. Reserve a static external IP in GCP Console
2. Create a managed SSL certificate for `ops.yourdomain.com`
3. Create a backend service pointing to the web VM on port 80
4. Create a URL map and HTTPS target proxy
5. Create a forwarding rule for the static IP
6. Point your domain's DNS A record at the static IP

The GCP LB terminates SSL. Nginx on the VM listens on port 80 only.

**Option B: Certbot (for non-GCP or simpler setups)**

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d ops.yourdomain.com
```

---

## Step 10: Configure Valor Webhooks (Optional)

If using Valor for billing:

1. Log into the Valor merchant portal
2. Go to Settings > Webhooks
3. Set webhook URL: `https://ops.yourdomain.com/webhooks/valor`
4. Select events: `subscription.cancelled`, `payment.failed`, `payment.success`
5. Copy the signing secret
6. Add to `.env` on both VMs: `VALOR_WEBHOOK_SECRET=<the-signing-secret>`
7. Restart the web server: `sudo supervisorctl restart ai-ops-web`

---

## Step 11: Verify Everything Works

```bash
# 1. Check web server health
curl https://ops.yourdomain.com/health
# Expected: {"status": "healthy", "supabase": "connected"}

# 2. Check worker health
cd /srv/ai-ops-saas/current
source /srv/ai-ops-saas/venv/bin/activate
python scripts/check_worker_health.py

# 3. Log into admin dashboard
# Open https://ops.yourdomain.com/admin/ in your browser
# Log in with the credentials from Step 4

# 4. Create a test tenant (from admin dashboard)
# Click "Add Tenant", fill in details, connect a test repo

# 5. Test bug intake
curl -X POST https://ops.yourdomain.com/api/v1/intake \
    -H "Content-Type: application/json" \
    -H "X-API-Key: <test-tenant-api-key>" \
    -d '{"error": "Test error from setup verification"}'
# Expected: {"session_id": "...", "status": "queued"}
```

---

## Updating

To deploy updates:

```bash
# On both VMs:
cd /srv/ai-ops-saas/current
git pull origin main
source /srv/ai-ops-saas/venv/bin/activate
pip install -r requirements.txt

# Web VM:
sudo supervisorctl restart ai-ops-web

# Worker VM:
sudo supervisorctl restart ai-ops-worker
```

---

## Workspace Maintenance

Set up a daily cron job to clean up workspaces:

```bash
crontab -e
# Add:
0 3 * * * /srv/ai-ops-saas/venv/bin/python /srv/ai-ops-saas/current/scripts/workspace_maintenance.py
```

This prunes workspaces for cancelled tenants (after 30-day retention), runs `git gc` to reclaim disk space, and alerts if any workspace exceeds 5 GB.

---

## Troubleshooting

**Worker not picking up tasks:**
- Check `sudo supervisorctl status ai-ops-worker`
- Check `/var/log/ai-ops/worker-error.log`
- Verify `.env` has correct `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`

**Claude agents failing:**
- Verify `claude --version` works on the worker VM
- Verify the `ai-ops` user has access to the Claude Code CLI
- Check that `CI=true` and `TERM=dumb` are set in the supervisor config

**Git clone failures:**
- Check the tenant's git credentials are valid
- Verify the worker user has SSH access (if using SSH URLs)
- Check `/var/log/ai-ops/worker.log` for git error messages

**Supabase connection errors:**
- Verify `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`
- Check if Supabase project is paused (free tier pauses after inactivity)
