# AI Ops SaaS — Master Plan

**Repo:** github.com/IRakow/ai-ops-saas
**Local:** ~/Desktop/ai-ops-saas/
**Based on:** github.com/IRakow/ai-ops-debugger (standalone extraction)
**Started:** 2026-03-11

---

## Table of Contents

1. [Vision & Goals](#1-vision--goals)
2. [Business Model](#2-business-model)
3. [Architecture Overview](#3-architecture-overview)
4. [Multi-Tenancy Design](#4-multi-tenancy-design)
5. [Authentication & Authorization](#5-authentication--authorization)
6. [Git Workspace Management](#6-git-workspace-management)
7. [Agent Pipeline (Reused)](#7-agent-pipeline-reused)
8. [Database Schema](#8-database-schema)
9. [API Design](#9-api-design)
10. [Operator Admin Dashboard](#10-operator-admin-dashboard)
11. [Tenant Dashboard](#11-tenant-dashboard)
12. [Onboarding Flow](#12-onboarding-flow)
13. [Bug Intake Integration](#13-bug-intake-integration)
14. [Billing & Usage Tracking](#14-billing--usage-tracking)
15. [Notifications](#15-notifications)
16. [Security Model](#16-security-model)
17. [Infrastructure & Deployment](#17-infrastructure--deployment)
18. [Monitoring & Observability](#18-monitoring--observability)
19. [Installation Manual](#19-installation-manual)
20. [Client Onboarding Manual](#20-client-onboarding-manual)
21. [Project Structure](#21-project-structure)
22. [Implementation Tasks](#22-implementation-tasks)
23. [Task Progress Tracker](#23-task-progress-tracker)

---

## 1. Vision & Goals

### What This Is

AI Ops SaaS is a hosted multi-tenant version of the AI Ops Debugger. Instead of installing the debugger on each client's infrastructure, the operator (you) runs a single instance that serves multiple clients. Each client gets:

- A JavaScript snippet to paste into their app (auto-detects bugs)
- A web dashboard to report bugs, request features, review fixes
- Automatic bug investigation, fixing, testing, and deployment via Claude Opus agents
- Fix memory that gets smarter over time

The client never sees or touches the agent code, prompts, or pipeline logic. They interact with a web UI and receive fixes as git commits or pull requests.

### Goals

1. **Zero-install for clients** — paste a script tag and connect their GitHub repo, done
2. **Full feature parity** — every capability from the standalone version works in SaaS mode
3. **Tenant isolation** — clients cannot see each other's data, code, or agent activity
4. **Operator visibility** — single dashboard to manage all tenants, billing, system health
5. **Scalable** — handle 10+ concurrent tenants without workers stepping on each other
6. **Profitable** — clear cost tracking (Claude API tokens are the main expense) with margin built in
7. **Code protection** — client never gets source code; all intelligence runs on operator's server

### What Changes from Standalone

| Aspect | Standalone | SaaS |
|--------|-----------|------|
| Deployment | Client's server | Operator's server |
| Code access | Local filesystem (WORKING_DIR) | Git clone per tenant |
| Config | Single .env file | Per-tenant config in database |
| Auth | Single user table | Operator admins + tenant users |
| Worker | One worker, one codebase | One worker, N codebases (round-robin) |
| Billing | N/A | Usage tracking + Valor Payment Systems |
| Onboarding | CLI wizard (onboard.py) | Web-based wizard |
| Bug intake | Points to localhost | Points to SaaS domain per tenant |

### What Stays the Same

- The entire agent pipeline (5 specialists + consolidator + implementer + tester + validator + fixer + assessor)
- The consensus engine (3-agent debate for features)
- Fix memory and knowledge base
- All agent prompts and system instructions
- The browser smoke test system
- The notification system (email + SMS)
- The soak monitoring system
- All templates and frontend JS (scoped to tenant)

---

## 2. Business Model

### Pricing Tiers

| Plan | Monthly | Included | Overage | Target |
|------|---------|----------|---------|--------|
| **Starter** | $299/mo | 10 bug fixes, 2 features | $25/fix, $50/feature | Solo devs, small apps |
| **Pro** | $799/mo | 30 bug fixes, 10 features | $20/fix, $40/feature | Small teams, growing apps |
| **Enterprise** | $1,999/mo | Unlimited fixes + features | — | Agencies, large codebases |
| **Custom** | Negotiated | SLA, dedicated worker, priority queue | — | Enterprise clients |

### Cost Structure (Per Fix)

| Component | Estimated Cost |
|-----------|---------------|
| 5 Specialists (50 turns each × $0.015/1K input, $0.075/1K output) | ~$2.50 |
| Consolidator (40 turns) | ~$1.00 |
| Implementer (150 turns, heavy output) | ~$8.00 |
| Tester + Validator (50+40 turns) | ~$2.00 |
| Fixer (conditional, 80 turns) | ~$3.00 |
| Assessor (40 turns) | ~$1.00 |
| **Total per fix (no retry)** | **~$14.50** |
| **Total per fix (1 retry)** | **~$25.50** |
| **Total per fix (2 retries)** | **~$36.50** |

At $25/fix overage on Starter, margin is $10.50-$25+ per fix depending on retries. At $799/mo for 30 fixes, that's $26.63/fix — profitable even with retries.

### Revenue Tracking

Track per tenant per month:
- Agent runs attempted
- Agent runs successful (FIXED verdict)
- Agent runs partial/failed
- Total Claude API tokens consumed (input + output)
- Features planned (consensus engine runs)
- Features implemented
- Deploy count
- Active bugs detected by intake JS

---

## 3. Architecture Overview

### System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        OPERATOR'S SERVER                             │
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌────────────────────┐    │
│  │  Flask Web   │    │   Worker     │    │   Git Workspaces   │    │
│  │  Application │    │   Daemon     │    │                    │    │
│  │              │    │              │    │  /workspaces/       │    │
│  │  - Tenant UI │    │  - Polls     │    │    tenant-a/       │    │
│  │  - Admin UI  │    │    queue     │    │    tenant-b/       │    │
│  │  - API       │    │  - Loads     │    │    tenant-c/       │    │
│  │  - Webhooks  │    │    tenant    │    │                    │    │
│  │  - Bug intake│    │    config    │    │  (git clones,      │    │
│  │              │    │  - Runs      │    │   one per tenant)  │    │
│  │              │    │    agents    │    │                    │    │
│  │              │    │  - Pushes    │    │                    │    │
│  │              │    │    fixes     │    │                    │    │
│  └──────┬───────┘    └──────┬───────┘    └────────────────────┘    │
│         │                   │                                       │
│         └───────┬───────────┘                                       │
│                 │                                                    │
│         ┌───────▼────────┐                                          │
│         │   Supabase     │                                          │
│         │   (multi-      │                                          │
│         │    tenant)     │                                          │
│         └────────────────┘                                          │
└─────────────────────────────────────────────────────────────────────┘
         ▲              ▲                    ▲
         │              │                    │
    HTTPS (UI)     HTTPS (API)         Git (SSH/HTTPS)
         │              │                    │
┌────────┴──┐    ┌──────┴─────┐    ┌────────┴────────┐
│ Client's  │    │ Client's   │    │ Client's GitHub  │
│ Browser   │    │ App (bug   │    │ / GitLab repo   │
│ (dashboard│    │  intake JS)│    │                  │
│  login)   │    │            │    │                  │
└───────────┘    └────────────┘    └──────────────────┘
```

### Request Flow — Bug Report

```
1. Client's app throws JS error
2. bug-intake.js captures error + screenshot
3. POST to https://ops.yourdomain.com/api/v1/intake/{tenant_api_key}
4. Flask validates API key, resolves tenant
5. Creates session + message in Supabase (scoped to tenant_id)
6. Enqueues agent task in ai_ops_agent_queue (with tenant_id)
7. Worker picks up task, loads tenant config
8. Worker does `git pull` in /workspaces/{tenant_id}/
9. 5 specialists run against tenant's codebase
10. Consolidator synthesizes → understanding stored
11. Implementer writes fix in workspace
12. Tester + Validator verify
13. Assessor gives verdict
14. If FIXED: git commit + push/PR to tenant's repo
15. Notification sent to tenant (email/SMS/webhook)
16. Usage recorded for billing
```

### Request Flow — Feature Request

```
1. Client logs into dashboard, clicks "I want something new"
2. Describes feature in chat
3. Submits to agent pipeline
4. Consensus engine (Architect + Engineer + QA) debates
5. Plan presented to client for approval
6. Client approves → Implementer builds it
7. Same test/validate/assess cycle
8. Fix pushed as PR to tenant's repo
```

---

## 4. Multi-Tenancy Design

### Tenant Model

Every tenant represents one client with one codebase. A tenant has:

```
tenant:
  id: UUID
  name: "Acme Corp"
  slug: "acme-corp" (URL-safe, unique)
  plan: "starter" | "pro" | "enterprise" | "custom"
  status: "active" | "suspended" | "trial" | "cancelled"

  # Git access
  git_repo_url: "https://github.com/acme/webapp.git"
  git_provider: "github" | "gitlab" | "bitbucket" | "other"
  git_credentials_encrypted: "..." (deploy key or token, encrypted at rest)
  git_default_branch: "main"
  git_deploy_branch: "main" (branch agents push to)

  # Workspace
  workspace_path: "/srv/ai-ops/workspaces/acme-corp/"
  last_git_sync: timestamp

  # Codebase context (replaces standalone codebase_context.md)
  codebase_context: TEXT (markdown, stored in DB)
  blast_radius: JSONB (per-module file allowlist)
  agent_protocol: TEXT (custom rules for agents)
  manifest: JSONB (cached codebase manifest)

  # App info (for agent prompts)
  app_name: "Acme Webapp"
  app_description: "A SaaS platform for managing widgets"
  app_url: "https://acme.com"
  app_stack: "Next.js, TypeScript, Prisma, PostgreSQL"

  # Deploy config
  deploy_method: "github_pr" | "git_push" | "ssh_script" | "webhook" | "none"
  deploy_config: JSONB (method-specific settings)

  # Notifications
  notification_emails: ["dev@acme.com", "cto@acme.com"]
  notification_webhook_url: "https://acme.com/webhooks/ai-ops"
  notification_slack_webhook: "https://hooks.slack.com/..."

  # Billing
  valor_customer_id: "..."
  valor_subscription_id: "..."
  billing_email: "billing@acme.com"
  monthly_fix_limit: 10
  monthly_feature_limit: 2
  fixes_used_this_month: 3
  features_used_this_month: 0

  # API
  api_key: "aops_..." (for bug intake + programmatic access)
  api_key_hash: "sha256:..." (stored hashed)

  # Metadata
  created_at: timestamp
  updated_at: timestamp
  trial_ends_at: timestamp (14-day trial)
  onboarded_at: timestamp (completed onboarding wizard)
```

### Tenant Isolation

1. **Database** — Every query includes `WHERE tenant_id = ?`. No exceptions. A middleware function `get_current_tenant()` returns the tenant from the request context (session for web, API key for API).

2. **Filesystem** — Each tenant's workspace is a separate directory. Agents run with `WORKING_DIR` set to the tenant's workspace. No cross-workspace access.

3. **Git** — Each tenant's credentials are encrypted separately. Git operations use tenant-specific credentials.

4. **Agent context** — Each agent run loads the tenant's `codebase_context`, `blast_radius`, and `agent_protocol` from the database. No shared context between tenants.

5. **Fix memory** — Per-tenant. The `ai_ops_fix_patterns` table gets a `tenant_id` column. Each tenant builds their own knowledge base.

6. **Queue priority** — Fair scheduling. Worker processes tasks in round-robin by tenant, not FIFO globally. No single tenant can starve others.

### Config Loading

The standalone version uses `config.py` which reads from `.env`. The SaaS version keeps `config.py` for operator-level settings (Supabase URL, Claude model, server paths) but loads tenant-specific settings from the database:

```python
# Operator config (from .env)
SUPABASE_URL = os.getenv("SUPABASE_URL")
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-opus-4-6")
WORKSPACE_BASE = os.getenv("WORKSPACE_BASE", "/srv/ai-ops/workspaces")

# Tenant config (from database, loaded per-request)
def get_tenant_config(tenant_id: str) -> TenantConfig:
    """Load tenant settings from Supabase."""
    tenant = supabase.table("tenants").select("*").eq("id", tenant_id).single().execute()
    return TenantConfig(
        working_dir=f"{WORKSPACE_BASE}/{tenant.data['slug']}/",
        app_name=tenant.data["app_name"],
        codebase_context=tenant.data["codebase_context"],
        blast_radius=tenant.data["blast_radius"],
        # ... all other tenant settings
    )
```

---

## 5. Authentication & Authorization

### Three Auth Levels

| Level | Who | Access |
|-------|-----|--------|
| **Operator Admin** | You (the SaaS owner) | All tenants, billing, system config, worker management |
| **Tenant Admin** | Client's team lead/CTO | Their tenant's settings, users, git config, billing |
| **Tenant User** | Client's developers | Report bugs, request features, review fixes, view history |

### Auth Implementation

**Operator admin** — Separate login page (`/admin/login`). Stored in `ai_ops_operator_admins` table. Session-based auth with `@operator_admin_required` decorator. Can impersonate any tenant for debugging.

**Tenant users** — Login at `/{tenant_slug}/login` or `/login` with tenant selection. Stored in `ai_ops_users` table with `tenant_id` column. Session stores `tenant_id`. All subsequent requests scoped to that tenant.

**API keys** — For bug intake and programmatic access. Format: `aops_live_{random_32_chars}`. Hashed with SHA-256 before storage. Included in `X-API-Key` header or `?api_key=` query param. Rate-limited per tenant.

### Session Structure

```python
# After tenant user login:
session["ai_ops_user_id"] = user.id
session["ai_ops_user_name"] = user.name
session["ai_ops_user_email"] = user.email
session["ai_ops_user_role"] = user.role  # "admin" or "user"
session["ai_ops_tenant_id"] = user.tenant_id
session["ai_ops_tenant_slug"] = tenant.slug

# After operator admin login:
session["operator_admin"] = True
session["operator_admin_id"] = admin.id
session["operator_admin_name"] = admin.name
# Can set session["ai_ops_tenant_id"] to impersonate
```

### Route Protection

```python
@ai_ops_bp.route("/sessions")
@tenant_login_required  # Checks session has tenant_id + user_id
def sessions():
    tenant_id = session["ai_ops_tenant_id"]
    sessions = service.get_sessions(tenant_id=tenant_id)
    ...

@admin_bp.route("/tenants")
@operator_admin_required  # Checks session has operator_admin = True
def tenants():
    all_tenants = service.get_all_tenants()
    ...
```

---

## 6. Git Workspace Management

### Workspace Lifecycle

```
Tenant created → Clone repo → Generate context → Generate manifest → Ready
                     │
                     ▼
              /srv/ai-ops/workspaces/{tenant-slug}/
              ├── .git/
              ├── (entire repo contents)
              └── .ai-ops-manifest.json (generated)
```

### Git Operations

**Initial clone (on tenant creation):**
```bash
git clone --depth 50 {repo_url} /srv/ai-ops/workspaces/{slug}/
```
Shallow clone to save disk. Depth 50 gives enough history for git blame.

**Pre-agent sync (before every agent run):**
```bash
cd /srv/ai-ops/workspaces/{slug}/
git fetch origin {default_branch}
git reset --hard origin/{default_branch}
git clean -fd
```
Hard reset ensures clean state. Any uncommitted agent work from a failed run gets wiped.

**Post-fix delivery (after FIXED verdict):**

Option A — Direct push:
```bash
git add -A
git commit -m "fix: {bug_description} [AI Ops #{session_id}]"
git push origin {deploy_branch}
```

Option B — Pull request (recommended):
```bash
git checkout -b ai-ops/fix-{session_id}
git add -A
git commit -m "fix: {bug_description} [AI Ops #{session_id}]"
git push origin ai-ops/fix-{session_id}
# Then use GitHub/GitLab API to create PR
```

Option C — Webhook (tenant's CI handles it):
```bash
# Commit locally, POST diff to tenant's webhook URL
```

### Credential Storage

Git credentials are encrypted at rest using Fernet (symmetric encryption). The encryption key is derived from the operator's `SECRET_KEY` environment variable.

```python
from cryptography.fernet import Fernet
import hashlib, base64

def _get_fernet():
    key = hashlib.sha256(config.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))

def encrypt_credential(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()

def decrypt_credential(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()
```

Supported credential types:
- **GitHub App** (recommended) — Install AI Ops GitHub App on their repo, get installation token
- **Deploy key** — SSH key pair generated per tenant, public key added to their repo
- **Personal access token** — Client provides a PAT with repo scope
- **OAuth token** — Via GitHub/GitLab OAuth flow during onboarding

### Workspace Maintenance

Cron job runs daily:
1. Prune workspaces for cancelled tenants (after 30-day retention)
2. Run `git gc` on all workspaces to save disk
3. Check disk usage per tenant, alert if >5GB
4. Verify git remotes are still accessible

---

## 7. Agent Pipeline (Reused)

The entire agent pipeline from the standalone version is reused with one key change: **config loading is per-tenant instead of from .env**.

### What Changes

The worker's main loop becomes:

```python
# STANDALONE VERSION:
while True:
    task = poll_queue()
    if task:
        run_agent_pipeline(task)  # Uses global config

# SAAS VERSION:
while True:
    task = poll_queue()  # Queue now has tenant_id
    if task:
        tenant = load_tenant(task.tenant_id)
        with tenant_context(tenant):  # Sets WORKING_DIR, context, etc.
            run_agent_pipeline(task)
```

### `tenant_context` Manager

```python
@contextmanager
def tenant_context(tenant: TenantConfig):
    """Set up environment for a specific tenant's agent run."""
    original_env = os.environ.copy()
    try:
        os.environ["WORKING_DIR"] = tenant.working_dir
        os.environ["APP_NAME"] = tenant.app_name
        os.environ["APP_BASE_URL"] = tenant.app_url

        # Write tenant's context file to workspace
        context_path = Path(tenant.working_dir) / ".ai-ops-context.md"
        context_path.write_text(tenant.codebase_context)

        # Write tenant's blast radius to workspace
        if tenant.blast_radius:
            br_path = Path(tenant.working_dir) / ".ai-ops-blast-radius.json"
            br_path.write_text(json.dumps(tenant.blast_radius))

        yield
    finally:
        os.environ.clear()
        os.environ.update(original_env)
```

### Fair Queue Processing

```python
def poll_queue_fair() -> Task | None:
    """Pick the next task, rotating between tenants."""
    # Get all pending tasks ordered by created_at
    tasks = supabase.table("ai_ops_agent_queue") \
        .select("*, tenants!inner(status, plan)") \
        .eq("status", "pending") \
        .eq("tenants.status", "active") \
        .order("created_at") \
        .execute()

    if not tasks.data:
        return None

    # Group by tenant, pick the one with oldest unprocessed task
    # that hasn't been served recently
    # (prevents one tenant flooding the queue)
    return select_fair_task(tasks.data)
```

### Specialist Prompts

Each specialist prompt gets the tenant's context injected:

```python
def build_specialist_prompt(specialist_type: str, tenant: TenantConfig, bug: dict) -> str:
    context = tenant.codebase_context
    protocol = tenant.agent_protocol or ""

    return f"""You are the {specialist_type} for {tenant.app_name}.

CODEBASE CONTEXT:
{context}

{f"AGENT PROTOCOL:{chr(10)}{protocol}" if protocol else ""}

BUG REPORT:
{bug['description']}

{bug.get('error_details', '')}
"""
```

### Everything Else

These modules are used as-is from the standalone version:
- `ai_ops_service.py` — add `tenant_id` param to all queries
- `ai_ops_orchestrator.py` — pass tenant config instead of global config
- `ai_ops_prompts.py` — already templates, just inject tenant values
- `ai_ops_knowledge_service.py` — add `tenant_id` to fix patterns
- `ai_ops_notes_service.py` — add `tenant_id` to queries
- `consensus_engine.py` — pass tenant context instead of global
- `triage_agent.py` — same
- `resilience.py` — already generic
- `notifications.py` — send to tenant's notification config
- `claude_wrapper.py` — already generic
- `fix_memory.py` — add `tenant_id` scope

---

## 8. Database Schema

### New Tables

#### `tenants`
```sql
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    plan VARCHAR(20) NOT NULL DEFAULT 'trial',
    status VARCHAR(20) NOT NULL DEFAULT 'trial',

    -- Git access
    git_repo_url TEXT,
    git_provider VARCHAR(20) DEFAULT 'github',
    git_credentials_encrypted TEXT,
    git_default_branch VARCHAR(100) DEFAULT 'main',
    git_deploy_branch VARCHAR(100) DEFAULT 'main',

    -- Workspace
    workspace_path TEXT,
    last_git_sync TIMESTAMPTZ,

    -- Codebase context
    codebase_context TEXT,
    blast_radius JSONB DEFAULT '{}',
    agent_protocol TEXT,
    manifest JSONB DEFAULT '{}',

    -- App info
    app_name VARCHAR(255),
    app_description TEXT,
    app_url TEXT,
    app_stack TEXT,

    -- Deploy
    deploy_method VARCHAR(20) DEFAULT 'github_pr',
    deploy_config JSONB DEFAULT '{}',

    -- Notifications
    notification_emails TEXT[] DEFAULT '{}',
    notification_webhook_url TEXT,
    notification_slack_webhook TEXT,

    -- Billing
    valor_customer_id VARCHAR(255),
    valor_subscription_id VARCHAR(255),
    billing_email VARCHAR(255),
    monthly_fix_limit INTEGER DEFAULT 10,
    monthly_feature_limit INTEGER DEFAULT 2,

    -- API
    api_key_hash VARCHAR(255) UNIQUE,
    api_key_prefix VARCHAR(12),

    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    trial_ends_at TIMESTAMPTZ,
    onboarded_at TIMESTAMPTZ
);

CREATE INDEX idx_tenants_slug ON tenants(slug);
CREATE INDEX idx_tenants_api_key_prefix ON tenants(api_key_prefix);
CREATE INDEX idx_tenants_status ON tenants(status);
```

#### `operator_admins`
```sql
CREATE TABLE operator_admins (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    is_super_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login_at TIMESTAMPTZ
);
```

#### `usage_records`
```sql
CREATE TABLE usage_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    session_id UUID REFERENCES ai_ops_sessions(id),

    record_type VARCHAR(20) NOT NULL, -- 'bug_fix', 'feature', 'retry'
    status VARCHAR(20) NOT NULL, -- 'started', 'completed', 'failed'

    -- Token usage
    input_tokens BIGINT DEFAULT 0,
    output_tokens BIGINT DEFAULT 0,
    total_cost_cents INTEGER DEFAULT 0, -- estimated cost in cents

    -- Timing
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_seconds INTEGER,

    -- Agent details
    agents_used JSONB DEFAULT '[]', -- list of agent types + turn counts
    retries INTEGER DEFAULT 0,
    verdict VARCHAR(20), -- 'FIXED', 'PARTIAL', 'FAILED'

    -- Billing
    billed BOOLEAN DEFAULT FALSE,
    billed_at TIMESTAMPTZ,
    invoice_id VARCHAR(255),

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_usage_tenant ON usage_records(tenant_id);
CREATE INDEX idx_usage_tenant_month ON usage_records(tenant_id, created_at);
CREATE INDEX idx_usage_billed ON usage_records(billed) WHERE billed = FALSE;
```

#### `tenant_api_keys`
```sql
CREATE TABLE tenant_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    name VARCHAR(255) NOT NULL, -- "Production", "Staging", "CI/CD"
    key_hash VARCHAR(255) NOT NULL UNIQUE,
    key_prefix VARCHAR(12) NOT NULL, -- "aops_live_abc" for display
    scopes TEXT[] DEFAULT '{intake,read}', -- 'intake', 'read', 'write', 'admin'
    is_active BOOLEAN DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX idx_api_keys_hash ON tenant_api_keys(key_hash);
CREATE INDEX idx_api_keys_tenant ON tenant_api_keys(tenant_id);
```

#### `webhooks`
```sql
CREATE TABLE webhooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    url TEXT NOT NULL,
    events TEXT[] NOT NULL, -- 'bug.detected', 'fix.completed', 'fix.failed', 'deploy.success'
    secret VARCHAR(255), -- for HMAC signature verification
    is_active BOOLEAN DEFAULT TRUE,
    last_triggered_at TIMESTAMPTZ,
    failure_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_webhooks_tenant ON webhooks(tenant_id);
```

### Modified Tables (from standalone)

All existing AI Ops tables get a `tenant_id` column:

```sql
-- Add tenant_id to all existing tables
ALTER TABLE ai_ops_users ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_sessions ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_messages ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_tasks ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_files ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_audit_log ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_agent_queue ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_notes ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_note_suggestions ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE ai_ops_fix_patterns ADD COLUMN tenant_id UUID REFERENCES tenants(id);

-- Add indexes
CREATE INDEX idx_sessions_tenant ON ai_ops_sessions(tenant_id);
CREATE INDEX idx_queue_tenant ON ai_ops_agent_queue(tenant_id);
CREATE INDEX idx_users_tenant ON ai_ops_users(tenant_id);
CREATE INDEX idx_fix_patterns_tenant ON ai_ops_fix_patterns(tenant_id);
CREATE INDEX idx_notes_tenant ON ai_ops_notes(tenant_id);
```

### Row-Level Security

```sql
-- Enable RLS on all tenant-scoped tables
ALTER TABLE ai_ops_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_ops_messages ENABLE ROW LEVEL SECURITY;
-- ... etc for all tables

-- Policy: service role bypasses RLS (for worker)
-- API calls use tenant_id filtering in application code
-- This is defense-in-depth, not the primary isolation mechanism
```

---

## 9. API Design

### Public API (for clients)

Base URL: `https://ops.yourdomain.com/api/v1`
Auth: `X-API-Key: aops_live_...` header

#### Bug Intake
```
POST /api/v1/intake
  Body: { error, url, screenshot_base64, user_agent, metadata }
  → Creates session + enqueues agent task
  ← { session_id, status: "queued" }
```

#### Sessions
```
GET  /api/v1/sessions
  ← List of sessions for this tenant

GET  /api/v1/sessions/{id}
  ← Session detail with messages, status, verdict

GET  /api/v1/sessions/{id}/diff
  ← Git diff of the fix

POST /api/v1/sessions/{id}/approve
  Body: { action: "approve" | "reject" | "retry" }
  ← { status: "approved" }
```

#### Status
```
GET  /api/v1/status
  ← { tenant: "active", queue_depth: 2, active_agents: 1 }
```

#### Webhooks (tenant manages their own)
```
GET    /api/v1/webhooks
POST   /api/v1/webhooks
  Body: { url, events: ["fix.completed", "bug.detected"] }
DELETE /api/v1/webhooks/{id}
```

### Operator Admin API

Base URL: `https://ops.yourdomain.com/admin/api`
Auth: Operator admin session cookie

```
GET    /admin/api/tenants
POST   /admin/api/tenants
GET    /admin/api/tenants/{id}
PATCH  /admin/api/tenants/{id}
DELETE /admin/api/tenants/{id}

GET    /admin/api/tenants/{id}/usage
GET    /admin/api/tenants/{id}/sessions
POST   /admin/api/tenants/{id}/sync-git

GET    /admin/api/system/health
GET    /admin/api/system/worker-status
GET    /admin/api/system/queue

GET    /admin/api/billing/overview
GET    /admin/api/billing/invoices
```

### Webhook Events

When a webhook-subscribed event occurs, POST to the tenant's webhook URL:

```json
{
    "event": "fix.completed",
    "timestamp": "2026-03-11T15:30:00Z",
    "tenant_id": "...",
    "session_id": "...",
    "data": {
        "verdict": "FIXED",
        "description": "Fixed null pointer in checkout flow",
        "pr_url": "https://github.com/acme/webapp/pull/42",
        "files_changed": ["src/checkout.py", "src/cart.py"],
        "agent_time_seconds": 340
    }
}
```

Signed with HMAC-SHA256 using the webhook's secret:
```
X-AI-Ops-Signature: sha256=abc123...
```

---

## 10. Operator Admin Dashboard

### Pages

1. **Dashboard** (`/admin/`)
   - Total tenants (active, trial, suspended)
   - Today's agent runs (started, completed, failed)
   - Queue depth (pending tasks across all tenants)
   - Worker status (running, last heartbeat)
   - Revenue this month
   - Top tenants by usage

2. **Tenants** (`/admin/tenants`)
   - List all tenants with status, plan, usage this month
   - Search/filter by name, status, plan
   - Quick actions: suspend, activate, impersonate

3. **Tenant Detail** (`/admin/tenants/{slug}`)
   - Full tenant info (editable)
   - Git connection status
   - Recent sessions
   - Usage chart (last 30 days)
   - Billing info
   - API keys
   - Action buttons: sync git, regenerate context, test connection

4. **Queue** (`/admin/queue`)
   - All pending/active tasks across tenants
   - Which tenant, what type, how long in queue
   - Can prioritize or cancel tasks

5. **Billing** (`/admin/billing`)
   - Revenue overview (MRR, per-tenant)
   - Unbilled usage
   - Invoice history
   - Plan distribution (how many on each tier)

6. **System** (`/admin/system`)
   - Worker health (last poll, tasks processed today)
   - Disk usage per workspace
   - Supabase connection status
   - Claude API status
   - Error log (last 100 errors)

7. **Settings** (`/admin/settings`)
   - Default plans and limits
   - Notification templates
   - System email/SMS config
   - Agent model selection
   - Agent timeout settings

### Design

Dark sidebar nav (consistent with existing AI Ops aesthetic). Steel blue accents. Tables with sorting and filtering. Charts using Chart.js.

---

## 11. Tenant Dashboard

The tenant dashboard is the existing AI Ops UI from the standalone version, with these additions:

### New Pages

1. **Settings** (`/{tenant}/settings`)
   - Repo connection (change URL, re-auth)
   - Notification preferences
   - Team members (add/remove users)
   - API keys (generate, revoke)
   - Webhooks (add, remove, test)
   - Deploy method selection
   - Codebase context (view/edit the auto-generated markdown)
   - Blast radius config (which files agents can touch)

2. **Usage** (`/{tenant}/usage`)
   - Fixes used vs limit
   - Features used vs limit
   - Agent time breakdown
   - Cost estimate

3. **Integrations** (`/{tenant}/integrations`)
   - Bug intake JS snippet (copy-paste ready, with tenant API key embedded)
   - GitHub App install link
   - Slack integration setup
   - CI/CD webhook URL

### Modified Pages

- **Dashboard** — Add usage widget (5/10 fixes used this month)
- **Session** — Show "Delivered as PR #42" link when fix is pushed
- **Status** — Show git operations (pulling latest, pushing fix)

---

## 12. Onboarding Flow

### Web-Based Wizard (5 Steps)

When a tenant admin first logs in (or is invited), they see the onboarding wizard:

**Step 1: Welcome + Plan Selection**
- Brief explanation of what AI Ops does
- Select plan (or start 14-day trial)
- Enter billing email

**Step 2: Connect Repository**
- Option A: Install GitHub App (recommended — one-click)
- Option B: Paste a personal access token + repo URL
- Option C: Generate SSH deploy key (we show the public key, they add to repo)
- Validate connection: test clone access
- Auto-detect default branch

**Step 3: Scan Codebase**
- After git clone succeeds, run `generate_context.py` against the workspace
- Show the auto-detected stack, structure, patterns
- Let them edit the codebase context markdown
- Generate blast radius from detected modules
- Generate manifest

**Step 4: Configure Delivery**
- How should fixes be delivered?
  - Pull request (default, safest)
  - Direct push to branch
  - Webhook notification only (manual apply)
- Set deploy branch
- Test: create a test branch, push empty commit, verify access

**Step 5: Set Up Bug Detection**
- Show the bug-intake.js snippet with their API key pre-filled
- Copy-paste instructions for their base template
- Test button: click to trigger a fake error and verify intake works
- Optional: configure notification preferences (email, Slack, webhook)

After completing all 5 steps, `onboarded_at` is set and they land on the dashboard.

### CLI Onboarding (Alternative)

For technical users who prefer the terminal:

```bash
# Install the AI Ops CLI client
pip install ai-ops-client

# Authenticate
ai-ops auth --server https://ops.yourdomain.com --api-key aops_live_...

# Scan and upload context
ai-ops scan /path/to/project

# Test connection
ai-ops test

# Report a bug
ai-ops report "Login page returns 500 after password reset"
```

---

## 13. Bug Intake Integration

### How It Works for SaaS

The client pastes this into their HTML:

```html
<!-- AI Ops Bug Detection -->
<link rel="stylesheet" href="https://ops.yourdomain.com/static/css/bug-intake.css">
<script>
  window.AI_OPS_CONFIG = {
    endpoint: "https://ops.yourdomain.com/api/v1/intake",
    apiKey: "aops_live_abc123...",
    appName: "Acme Webapp"
  };
</script>
<script src="https://ops.yourdomain.com/static/js/bug-intake.js"></script>
```

The `bug-intake.js` is served from the SaaS server (not CDN, to ensure latest version). It:

1. Intercepts `fetch()` to detect 500+ responses
2. Listens for `window.onerror` (uncaught exceptions)
3. Listens for `unhandledrejection` (promise rejections)
4. Takes screenshots using html2canvas (loaded on demand)
5. POSTs to the SaaS intake endpoint with the API key
6. Shows a floating "Report Bug" button for manual reports

### Intake Endpoint

```python
@api_bp.route("/api/v1/intake", methods=["POST"])
@require_api_key(scopes=["intake"])
def intake():
    tenant = g.tenant  # Set by require_api_key middleware

    # Rate limit: 100 reports per hour per tenant
    if rate_limited(tenant.id, "intake", limit=100, window=3600):
        return jsonify({"error": "Rate limited"}), 429

    data = request.json
    session = create_intake_session(
        tenant_id=tenant.id,
        error=data.get("error"),
        url=data.get("url"),
        screenshot=data.get("screenshot_base64"),
        user_agent=data.get("user_agent"),
        metadata=data.get("metadata", {}),
    )

    # Auto-enqueue for agent processing
    enqueue_task(
        tenant_id=tenant.id,
        session_id=session.id,
        task_type="bug",
        description=data.get("error", "Auto-detected error"),
    )

    return jsonify({"session_id": session.id, "status": "queued"}), 201
```

### CORS Configuration

The intake endpoint must accept cross-origin requests from client apps:

```python
@app.after_request
def add_cors_headers(response):
    if request.path.startswith("/api/v1/intake"):
        origin = request.headers.get("Origin", "*")
        # Validate origin belongs to a known tenant
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    return response
```

---

## 14. Billing & Usage Tracking

### Usage Tracking

Every agent run creates a `usage_record`:

```python
def track_usage(tenant_id: str, session_id: str, record_type: str):
    """Called at the start of an agent pipeline run."""
    return supabase.table("usage_records").insert({
        "tenant_id": tenant_id,
        "session_id": session_id,
        "record_type": record_type,
        "status": "started",
    }).execute()

def complete_usage(record_id: str, verdict: str, tokens: dict, duration: int):
    """Called when the pipeline finishes."""
    cost = estimate_cost(tokens)
    supabase.table("usage_records").update({
        "status": "completed",
        "verdict": verdict,
        "input_tokens": tokens["input"],
        "output_tokens": tokens["output"],
        "total_cost_cents": cost,
        "duration_seconds": duration,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", record_id).execute()
```

### Limit Enforcement

Before starting an agent run, check the tenant's usage:

```python
def check_usage_limits(tenant: TenantConfig, task_type: str) -> bool:
    """Returns True if the tenant can run another task."""
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0)

    usage = supabase.table("usage_records") \
        .select("record_type", count="exact") \
        .eq("tenant_id", tenant.id) \
        .eq("record_type", task_type) \
        .gte("created_at", month_start.isoformat()) \
        .neq("status", "failed") \
        .execute()

    if task_type == "bug_fix":
        return usage.count < tenant.monthly_fix_limit
    elif task_type == "feature":
        return usage.count < tenant.monthly_feature_limit

    return True
```

### Valor Payment Systems Integration

Valor handles recurring subscription billing and overage charges. The integration uses Valor's REST API for customer/subscription management and webhooks for payment event notifications.

```python
import requests

VALOR_API_BASE = config.VALOR_API_BASE  # e.g. "https://api.valorpaytech.com/v1"
VALOR_API_KEY = config.VALOR_API_KEY
VALOR_APP_ID = config.VALOR_APP_ID

def _valor_headers() -> dict:
    return {
        "Authorization": f"Bearer {VALOR_API_KEY}",
        "X-App-Id": VALOR_APP_ID,
        "Content-Type": "application/json",
    }

def create_tenant_subscription(tenant_id: str, plan: str, billing_email: str):
    """Create Valor customer + recurring subscription for a new tenant."""
    # Create customer in Valor
    customer = requests.post(
        f"{VALOR_API_BASE}/customers",
        headers=_valor_headers(),
        json={"email": billing_email, "description": f"AI Ops tenant: {tenant_id}"},
    ).json()

    plan_amounts = {
        "starter": 29900,   # $299.00 in cents
        "pro": 79900,       # $799.00
        "enterprise": 199900,  # $1,999.00
    }

    # Create recurring subscription
    subscription = requests.post(
        f"{VALOR_API_BASE}/subscriptions",
        headers=_valor_headers(),
        json={
            "customer_id": customer["id"],
            "amount": plan_amounts[plan],
            "interval": "monthly",
            "trial_days": 14,
            "description": f"AI Ops {plan.title()} Plan",
        },
    ).json()

    supabase.table("tenants").update({
        "valor_customer_id": customer["id"],
        "valor_subscription_id": subscription["id"],
    }).eq("id", tenant_id).execute()

def charge_overage(tenant_id: str, task_type: str, count: int = 1):
    """Charge overage fee via Valor for usage beyond plan limits."""
    tenant = load_tenant(tenant_id)

    overage_amounts = {
        "bug_fix": 2500,   # $25.00
        "feature": 5000,   # $50.00
    }

    requests.post(
        f"{VALOR_API_BASE}/charges",
        headers=_valor_headers(),
        json={
            "customer_id": tenant.valor_customer_id,
            "amount": overage_amounts[task_type] * count,
            "description": f"AI Ops overage: {count}x {task_type}",
        },
    )
```

### Valor Webhooks

```python
@billing_bp.route("/webhooks/valor", methods=["POST"])
def valor_webhook():
    """Handle Valor payment events (success, failure, cancellation)."""
    payload = request.json
    sig = request.headers.get("X-Valor-Signature")

    # Verify HMAC signature
    expected = hmac.new(
        config.VALOR_WEBHOOK_SECRET.encode(),
        request.data,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig or "", expected):
        return jsonify({"error": "Invalid signature"}), 403

    event_type = payload.get("event")

    if event_type == "subscription.cancelled":
        tenant = find_tenant_by_valor_id(payload["customer_id"])
        suspend_tenant(tenant.id)

    elif event_type == "payment.failed":
        tenant = find_tenant_by_valor_id(payload["customer_id"])
        notify_payment_failed(tenant)

    elif event_type == "payment.success":
        tenant = find_tenant_by_valor_id(payload["customer_id"])
        activate_tenant(tenant.id)

    return "", 200
```

---

## 15. Notifications

### Notification Channels

| Channel | Tenant Config | Use Cases |
|---------|--------------|-----------|
| Email | `notification_emails` | Fix completed, fix failed, usage limit approaching |
| Slack | `notification_slack_webhook` | All events (real-time) |
| Webhook | Registered webhooks | Programmatic integration |
| SMS | Future (via Twilio) | Critical alerts only |

### Notification Events

| Event | Email | Slack | Webhook |
|-------|-------|-------|---------|
| `bug.detected` | No | Yes | Yes |
| `fix.started` | No | Yes | Yes |
| `fix.completed` | Yes | Yes | Yes |
| `fix.failed` | Yes | Yes | Yes |
| `feature.planned` | Yes | Yes | Yes |
| `feature.completed` | Yes | Yes | Yes |
| `deploy.success` | Yes | Yes | Yes |
| `deploy.failed` | Yes | Yes | Yes |
| `usage.limit_approaching` | Yes | Yes | No |
| `usage.limit_reached` | Yes | Yes | No |
| `trial.ending_soon` | Yes | No | No |
| `payment.failed` | Yes | No | No |

### Notification Templates

Stored as Jinja2 templates in `app/templates/notifications/`:

```
templates/notifications/
├── email/
│   ├── fix_completed.html
│   ├── fix_failed.html
│   ├── usage_warning.html
│   └── trial_ending.html
└── slack/
    ├── fix_completed.json
    ├── fix_failed.json
    └── bug_detected.json
```

---

## 16. Security Model

### Data Protection

1. **Git credentials** — Encrypted at rest with Fernet (AES-128-CBC). Decrypted only in memory during git operations.

2. **API keys** — Stored as SHA-256 hashes. Plaintext shown once on creation, never again.

3. **Client code** — Stored in tenant workspaces on operator's server. Access controlled by OS-level permissions. Each workspace owned by the worker process user.

4. **Session data** — Flask sessions use signed cookies with `SECRET_KEY`. Tenant ID stored in session to prevent cross-tenant access.

5. **Database** — Supabase with RLS as defense-in-depth. Primary isolation is application-level `tenant_id` filtering.

### Network Security

1. **HTTPS everywhere** — TLS termination at load balancer (GCP HTTPS LB or Cloudflare)
2. **CORS** — Intake endpoint allows cross-origin from known tenant domains only
3. **Rate limiting** — Per-tenant, per-endpoint limits (nginx + application-level)
4. **API key rotation** — Tenants can generate new keys and revoke old ones
5. **Webhook signatures** — HMAC-SHA256 for all outgoing webhooks

### Access Control

| Action | Operator Admin | Tenant Admin | Tenant User | API Key |
|--------|:---:|:---:|:---:|:---:|
| View all tenants | Y | | | |
| Manage tenant settings | Y | Y (own) | | |
| Manage users | Y | Y (own tenant) | | |
| View sessions | Y | Y | Y (own tenant) | Read scope |
| Report bug | Y | Y | Y | Intake scope |
| Approve fix | Y | Y | Y | Write scope |
| View billing | Y | Y | | |
| Manage API keys | Y | Y | | |
| System settings | Y | | | |

### Audit Trail

Every significant action logged:

```sql
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    actor_type VARCHAR(20) NOT NULL, -- 'operator', 'tenant_admin', 'tenant_user', 'api_key', 'system'
    actor_id UUID,
    action VARCHAR(100) NOT NULL, -- 'session.created', 'fix.approved', 'git.pushed', etc.
    resource_type VARCHAR(50),
    resource_id UUID,
    details JSONB DEFAULT '{}',
    ip_address INET,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_tenant ON audit_log(tenant_id, created_at);
```

---

## 17. Infrastructure & Deployment

### Production Setup

```
GCP Project: ai-ops-saas
Region: us-central1

┌─────────────────────────────────────────┐
│        GCP HTTPS Load Balancer           │
│        (SSL termination)                 │
└──────────┬──────────────┬───────────────┘
           │              │
    ┌──────▼──────┐  ┌────▼────────────┐
    │  Web VM     │  │  Worker VM       │
    │  (e2-std-2) │  │  (e2-std-4)      │
    │             │  │                   │
    │  nginx      │  │  supervisor       │
    │  gunicorn   │  │  ai-ops-worker    │
    │  (4 workers)│  │                   │
    │             │  │  /srv/ai-ops/     │
    │             │  │   workspaces/     │
    │             │  │    tenant-a/      │
    │             │  │    tenant-b/      │
    └──────┬──────┘  └────────┬─────────┘
           │                  │
           └─────┬────────────┘
                 │
         ┌───────▼────────┐
         │   Supabase      │
         │   (managed)     │
         └─────────────────┘
```

### Scaling Strategy

**Phase 1 (0-10 tenants):** Single web VM + single worker VM. Worker processes tasks sequentially.

**Phase 2 (10-50 tenants):** Same web VM. Add worker VMs (each handles N tenants). Round-robin assignment.

**Phase 3 (50+ tenants):** Multiple web VMs behind LB. Worker pool with task claiming (each worker picks unclaimed tasks). Workspace storage on network-attached SSD (or NFS).

### Cost Estimate (Phase 1)

| Component | Monthly Cost |
|-----------|-------------|
| Web VM (e2-standard-2) | ~$50 |
| Worker VM (e2-standard-4) | ~$100 |
| Supabase Pro | $25 |
| Domain + SSL (Cloudflare free) | $0 |
| Disk (100GB SSD for workspaces) | ~$17 |
| Claude API (pass-through) | Variable |
| **Total fixed** | **~$192/mo** |

Break-even: 1 Starter tenant ($299/mo) covers infrastructure with $107 margin.

### Deployment Process

```bash
# Web VM
cd /srv/ai-ops-saas/current
git pull origin main
pip install -r requirements.txt
sudo supervisorctl restart ai-ops-web

# Worker VM
cd /srv/ai-ops-saas/current
git pull origin main
pip install -r requirements.txt
sudo supervisorctl restart ai-ops-worker
```

---

## 18. Monitoring & Observability

### Health Checks

**Web health** (`/health`):
```json
{
    "status": "healthy",
    "supabase": "connected",
    "timestamp": "2026-03-11T15:30:00Z"
}
```

**Worker health** (written to Supabase every poll cycle):
```json
{
    "worker_id": "worker-1",
    "last_heartbeat": "2026-03-11T15:30:00Z",
    "tasks_processed_today": 12,
    "current_task": null,
    "uptime_seconds": 86400
}
```

### Alerts

| Condition | Alert | Channel |
|-----------|-------|---------|
| Worker heartbeat >5 min old | Worker down | Email + SMS to operator |
| Queue depth >20 | Queue backup | Email to operator |
| Supabase connection failed | DB down | SMS to operator |
| Tenant workspace disk >90% | Disk full | Email to operator |
| Fix success rate <50% (24h window) | Quality degraded | Email to operator |
| Claude API errors >5 in 1 hour | API issues | Email to operator |

### Logging

Structured JSON logs to `/var/log/ai-ops/`:

```json
{
    "timestamp": "2026-03-11T15:30:00Z",
    "level": "INFO",
    "tenant_id": "abc-123",
    "session_id": "def-456",
    "agent": "implementer",
    "message": "Fix completed, 3 files changed",
    "duration_ms": 45000
}
```

Log rotation: 7 days, compressed. Ship to GCP Cloud Logging for long-term storage.

---

## 19. Installation Manual

### For the Operator (You)

#### Prerequisites

- **GCP project** with billing enabled
- **2 VMs**: e2-standard-2 (web) + e2-standard-4 (worker)
  - Both running Ubuntu 22.04 LTS
  - Python 3.12+
  - Node.js 18+ (for Claude Code CLI)
- **Supabase project** (Pro plan recommended for production)
- **Domain name** pointed at GCP HTTPS LB (e.g., `ops.yourdomain.com`)
- **Claude Code CLI** installed and authenticated on the worker VM
- **Valor Payment Systems account** (for billing)
- **SendGrid account** (for transactional email)
- Optional: Twilio (SMS), Cloudflare (CDN/WAF)

#### Step 1: Clone the Repo

```bash
# On both VMs:
sudo mkdir -p /srv/ai-ops-saas
sudo chown $USER:$USER /srv/ai-ops-saas
git clone https://github.com/IRakow/ai-ops-saas.git /srv/ai-ops-saas/current
cd /srv/ai-ops-saas/current

python3.12 -m venv /srv/ai-ops-saas/venv
source /srv/ai-ops-saas/venv/bin/activate
pip install -r requirements.txt
```

#### Step 2: Configure Environment

```bash
cp .env.example .env
# Edit with your settings (see .env.example for all variables)
```

Required variables:
```env
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...

# Flask
SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")

# Valor Payment Systems
VALOR_API_BASE=https://api.valorpaytech.com/v1
VALOR_API_KEY=...
VALOR_APP_ID=...
VALOR_WEBHOOK_SECRET=...

# SendGrid
SENDGRID_API_KEY=SG...
NOTIFICATION_FROM_EMAIL=ops@yourdomain.com

# Claude
AGENT_MODEL=claude-opus-4-6

# Paths (worker VM)
WORKSPACE_BASE=/srv/ai-ops-saas/workspaces

# Domain
SAAS_DOMAIN=ops.yourdomain.com
```

#### Step 3: Run Migrations

```bash
# Run all migrations in order
for f in migrations/*.sql; do
    echo "Running $f..."
    psql $DATABASE_URL < "$f"
done
```

#### Step 4: Create Operator Admin

```bash
python scripts/create_operator_admin.py \
    --name "Your Name" \
    --email "you@yourdomain.com" \
    --password "your-secure-password"
```

#### Step 5: Set Up Valor Payment Products

```bash
python scripts/setup_valor_products.py
# Creates subscription plans in Valor, outputs IDs for .env
```

#### Step 6: Configure Nginx (Web VM)

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
    }
}
```

#### Step 7: Configure Supervisor

**Web VM** (`/etc/supervisor/conf.d/ai-ops-web.conf`):
```ini
[program:ai-ops-web]
command=/srv/ai-ops-saas/venv/bin/gunicorn "app:create_app()" -b 127.0.0.1:8000 -w 4 --timeout 120
directory=/srv/ai-ops-saas/current
user=www-data
autostart=true
autorestart=true
environment=PATH="/srv/ai-ops-saas/venv/bin:%(ENV_PATH)s"
stdout_logfile=/var/log/ai-ops/web.log
stderr_logfile=/var/log/ai-ops/web-error.log
```

**Worker VM** (`/etc/supervisor/conf.d/ai-ops-worker.conf`):
```ini
[program:ai-ops-worker]
command=/srv/ai-ops-saas/venv/bin/python worker.py
directory=/srv/ai-ops-saas/current
user=ai-ops
autostart=true
autorestart=true
environment=PATH="/srv/ai-ops-saas/venv/bin:/usr/local/bin:%(ENV_PATH)s",HOME="/home/ai-ops"
stdout_logfile=/var/log/ai-ops/worker.log
stderr_logfile=/var/log/ai-ops/worker-error.log
```

#### Step 8: Start Services

```bash
# Web VM
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start ai-ops-web

# Worker VM
sudo mkdir -p /srv/ai-ops-saas/workspaces
sudo chown ai-ops:ai-ops /srv/ai-ops-saas/workspaces
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start ai-ops-worker
```

#### Step 9: Set Up GCP HTTPS Load Balancer

1. Reserve a static external IP
2. Create an SSL certificate (managed by GCP or upload your own)
3. Create a backend service pointing to the web VM
4. Create a URL map and HTTPS proxy
5. Point your domain's DNS A record at the static IP

#### Step 10: Configure Valor Webhooks

In the Valor merchant portal:
- Webhook URL: `https://ops.yourdomain.com/webhooks/valor`
- Events: `subscription.cancelled`, `payment.failed`, `payment.success`
- Copy the signing secret to `VALOR_WEBHOOK_SECRET` in `.env`

#### Step 11: Verify

```bash
# Check web is running
curl https://ops.yourdomain.com/health

# Check worker is running
python scripts/check_worker_health.py

# Login to admin dashboard
open https://ops.yourdomain.com/admin/
```

---

## 20. Client Onboarding Manual

### For Your Clients

This is what you give to clients (or what they see in the onboarding wizard):

#### Getting Started with AI Ops

1. **Sign up** at `https://ops.yourdomain.com/signup`
2. **Connect your repo** — Click "Connect GitHub" and authorize the AI Ops GitHub App on your repository
3. **Review codebase scan** — We automatically scan your codebase and detect your stack, structure, and patterns. Edit the description if anything looks wrong.
4. **Configure delivery** — Choose how you want fixes delivered: Pull Request (recommended), direct push, or webhook only
5. **Add bug detection** — Paste this into your base HTML template:

```html
<link rel="stylesheet" href="https://ops.yourdomain.com/static/css/bug-intake.css">
<script>
  window.AI_OPS_CONFIG = {
    endpoint: "https://ops.yourdomain.com/api/v1/intake",
    apiKey: "YOUR_API_KEY_HERE"
  };
</script>
<script src="https://ops.yourdomain.com/static/js/bug-intake.js"></script>
```

6. **Test it** — Click the floating bug button on any page of your app and submit a test report. You should see it appear in your dashboard within 30 seconds.

7. **Watch agents work** — Go to your dashboard, click on the test session, and watch the agent pipeline investigate and fix (or determine it's a test).

#### Dashboard Overview

- **Sessions** — Every bug report and feature request creates a session
- **Status** — Real-time progress of agent pipeline (investigating → fixing → testing → deploying)
- **History** — All past sessions with verdicts and outcomes
- **Settings** — Manage repo connection, team members, notifications, API keys

#### API Access

For programmatic bug reporting (CI/CD integration, custom error handlers):

```python
import requests

response = requests.post(
    "https://ops.yourdomain.com/api/v1/intake",
    headers={"X-API-Key": "aops_live_..."},
    json={
        "error": "TypeError: Cannot read property 'id' of undefined",
        "url": "/dashboard",
        "metadata": {"user_id": "123", "environment": "production"}
    }
)
```

---

## 21. Project Structure

```
ai-ops-saas/
│
├── MASTER_PLAN.md                         # This file — persistent plan + tracker
├── README.md                              # Public-facing overview
├── SETUP_GUIDE.md                         # Operator installation guide
├── CLIENT_GUIDE.md                        # What to give clients
├── requirements.txt
├── .env.example
├── .gitignore
│
├── config.py                              # Operator-level config (from .env)
├── worker.py                              # Background daemon (multi-tenant)
├── claude_wrapper.py                      # Claude Code CLI wrapper
├── smoke_test.py                          # Post-deploy smoke test
├── manifest_generator.py                  # Codebase manifest builder
├── generate_context.py                    # Auto codebase scanner
│
├── app/
│   ├── __init__.py                        # Flask app factory
│   ├── supabase_client.py                 # Thread-safe Supabase client
│   ├── gemini_client.py                   # Gemini wrapper
│   ├── tenant.py                          # TenantConfig model + loader
│   ├── crypto.py                          # Credential encryption/decryption
│   │
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── ai_ops.py                      # Tenant dashboard routes (existing, add tenant scoping)
│   │   ├── admin.py                       # Operator admin routes (NEW)
│   │   ├── api.py                         # Public API routes (NEW)
│   │   ├── billing.py                     # Valor webhooks (NEW)
│   │   ├── bug_intake.py                  # Bug intake endpoint (modified for multi-tenant)
│   │   └── onboarding.py                  # Onboarding wizard routes (NEW)
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── ai_ops_service.py              # Core CRUD (add tenant_id)
│   │   ├── ai_ops_orchestrator.py         # Session lifecycle (tenant context)
│   │   ├── ai_ops_prompts.py              # Agent prompts (tenant context injection)
│   │   ├── ai_ops_notification_service.py # Notifications (per-tenant config)
│   │   ├── ai_ops_knowledge_service.py    # Fix patterns (per-tenant)
│   │   ├── ai_ops_notes_service.py        # Notes (per-tenant)
│   │   ├── bug_intake_service.py          # Bug intake backend
│   │   ├── tenant_service.py              # Tenant CRUD + git operations (NEW)
│   │   ├── billing_service.py             # Valor integration (NEW)
│   │   ├── usage_service.py               # Usage tracking + limits (NEW)
│   │   ├── webhook_service.py             # Outgoing webhook delivery (NEW)
│   │   └── git_service.py                 # Git clone/pull/push/PR (NEW)
│   │
│   ├── self_healing/
│   │   ├── __init__.py
│   │   ├── consensus_engine.py            # 3-agent consensus (tenant context)
│   │   ├── triage_agent.py
│   │   ├── resilience.py
│   │   └── notifications.py
│   │
│   ├── utils/
│   │   ├── ai_ops_auth.py                 # Tenant user auth decorator
│   │   ├── admin_auth.py                  # Operator admin auth decorator (NEW)
│   │   └── api_auth.py                    # API key auth middleware (NEW)
│   │
│   ├── templates/
│   │   ├── ai_ops/                        # Tenant dashboard (existing, modified)
│   │   │   ├── base_ai_ops.html
│   │   │   ├── login.html
│   │   │   ├── dashboard.html
│   │   │   ├── session.html
│   │   │   ├── plan.html
│   │   │   ├── status.html
│   │   │   ├── history.html
│   │   │   ├── diff_review.html
│   │   │   ├── report_card.html
│   │   │   ├── notes.html
│   │   │   ├── settings.html              # NEW
│   │   │   ├── usage.html                 # NEW
│   │   │   └── integrations.html          # NEW
│   │   │
│   │   ├── admin/                         # Operator dashboard (NEW)
│   │   │   ├── base_admin.html
│   │   │   ├── login.html
│   │   │   ├── dashboard.html
│   │   │   ├── tenants.html
│   │   │   ├── tenant_detail.html
│   │   │   ├── queue.html
│   │   │   ├── billing.html
│   │   │   ├── system.html
│   │   │   └── settings.html
│   │   │
│   │   ├── onboarding/                    # Onboarding wizard (NEW)
│   │   │   ├── base_onboarding.html
│   │   │   ├── welcome.html
│   │   │   ├── connect_repo.html
│   │   │   ├── scan_codebase.html
│   │   │   ├── configure_delivery.html
│   │   │   └── setup_detection.html
│   │   │
│   │   ├── notifications/                 # Email templates (NEW)
│   │   │   └── email/
│   │   │       ├── fix_completed.html
│   │   │       ├── fix_failed.html
│   │   │       ├── usage_warning.html
│   │   │       └── welcome.html
│   │   │
│   │   └── public/                        # Marketing / signup (NEW)
│   │       ├── signup.html
│   │       └── pricing.html
│   │
│   └── static/
│       ├── js/
│       │   ├── ai-ops-chat.js
│       │   ├── ai-ops-status.js
│       │   ├── bug-intake.js              # Modified for SaaS (configurable endpoint)
│       │   ├── notes-intake.js
│       │   ├── admin-dashboard.js         # NEW
│       │   └── onboarding.js              # NEW
│       ├── css/
│       │   ├── ai-ops.css
│       │   ├── bug-intake.css
│       │   ├── admin.css                  # NEW
│       │   └── onboarding.css             # NEW
│       └── vendor/
│           └── html2canvas/
│               └── html2canvas.min.js
│
├── tools/
│   ├── fix_memory.py
│   └── browser_smoke_test.py
│
├── migrations/
│   ├── 001_core_tables.sql                # Existing (from standalone)
│   ├── 002_agent_queue.sql                # Existing
│   ├── 003_notes_tables.sql               # Existing
│   ├── 004_fix_patterns.sql               # Existing
│   ├── 005_tenants.sql                    # NEW — tenants table
│   ├── 006_operator_admins.sql            # NEW
│   ├── 007_add_tenant_id.sql              # NEW — add tenant_id to all existing tables
│   ├── 008_api_keys.sql                   # NEW
│   ├── 009_usage_records.sql              # NEW
│   ├── 010_webhooks.sql                   # NEW
│   └── 011_audit_log.sql                  # NEW
│
├── scripts/
│   ├── create_operator_admin.py           # NEW
│   ├── setup_valor_products.py            # NEW
│   ├── check_worker_health.py             # NEW
│   ├── workspace_maintenance.py           # NEW — daily cron
│   └── migrate.py                         # NEW — run migrations
│
├── examples/
│   ├── blast_radius.example.json
│   ├── codebase_context.example.md
│   ├── deploy_script.example.sh
│   ├── supervisor.example.conf
│   ├── nginx.example.conf                 # NEW
│   └── client_snippet.example.html        # NEW
│
└── docs/
    ├── architecture.md
    ├── customization.md
    ├── api_reference.md                   # NEW
    ├── billing.md                         # NEW
    └── security.md                        # NEW
```

---

## 22. Implementation Tasks

### Phase 1: Foundation (Multi-Tenancy Core)

| # | Task | Status | Files |
|---|------|--------|-------|
| 1.1 | Copy all files from ai-ops-debugger repo as starting point | NOT STARTED | All files |
| 1.2 | Write migration 005_tenants.sql | NOT STARTED | migrations/005_tenants.sql |
| 1.3 | Write migration 006_operator_admins.sql | NOT STARTED | migrations/006_operator_admins.sql |
| 1.4 | Write migration 007_add_tenant_id.sql (add tenant_id to all existing tables) | NOT STARTED | migrations/007_add_tenant_id.sql |
| 1.5 | Write migration 008_api_keys.sql | NOT STARTED | migrations/008_api_keys.sql |
| 1.6 | Write migration 009_usage_records.sql | NOT STARTED | migrations/009_usage_records.sql |
| 1.7 | Write migration 010_webhooks.sql | NOT STARTED | migrations/010_webhooks.sql |
| 1.8 | Write migration 011_audit_log.sql | NOT STARTED | migrations/011_audit_log.sql |
| 1.9 | Create app/tenant.py (TenantConfig model + loader) | NOT STARTED | app/tenant.py |
| 1.10 | Create app/crypto.py (Fernet credential encryption) | NOT STARTED | app/crypto.py |
| 1.11 | Update config.py for SaaS (add WORKSPACE_BASE, SAAS_DOMAIN, Valor vars) | NOT STARTED | config.py |
| 1.12 | Update app/__init__.py (register new blueprints, tenant middleware) | NOT STARTED | app/__init__.py |

### Phase 2: Auth & Routing

| # | Task | Status | Files |
|---|------|--------|-------|
| 2.1 | Create app/utils/admin_auth.py (operator admin decorators) | NOT STARTED | app/utils/admin_auth.py |
| 2.2 | Create app/utils/api_auth.py (API key middleware) | NOT STARTED | app/utils/api_auth.py |
| 2.3 | Update app/utils/ai_ops_auth.py (add tenant scoping) | NOT STARTED | app/utils/ai_ops_auth.py |
| 2.4 | Create app/routes/admin.py (operator admin routes) | NOT STARTED | app/routes/admin.py |
| 2.5 | Create app/routes/api.py (public API routes) | NOT STARTED | app/routes/api.py |
| 2.6 | Create app/routes/billing.py (Valor webhook handler) | NOT STARTED | app/routes/billing.py |
| 2.7 | Create app/routes/onboarding.py (wizard routes) | NOT STARTED | app/routes/onboarding.py |
| 2.8 | Update app/routes/ai_ops.py (add tenant scoping to all routes) | NOT STARTED | app/routes/ai_ops.py |
| 2.9 | Update app/routes/bug_intake.py (API key auth, multi-tenant) | NOT STARTED | app/routes/bug_intake.py |

### Phase 3: Services

| # | Task | Status | Files |
|---|------|--------|-------|
| 3.1 | Create app/services/tenant_service.py (tenant CRUD + git ops) | NOT STARTED | app/services/tenant_service.py |
| 3.2 | Create app/services/git_service.py (clone, pull, push, PR) | NOT STARTED | app/services/git_service.py |
| 3.3 | Create app/services/billing_service.py (Valor integration) | NOT STARTED | app/services/billing_service.py |
| 3.4 | Create app/services/usage_service.py (tracking + limits) | NOT STARTED | app/services/usage_service.py |
| 3.5 | Create app/services/webhook_service.py (outgoing webhooks) | NOT STARTED | app/services/webhook_service.py |
| 3.6 | Update ai_ops_service.py (add tenant_id to all queries) | NOT STARTED | app/services/ai_ops_service.py |
| 3.7 | Update ai_ops_orchestrator.py (tenant context injection) | NOT STARTED | app/services/ai_ops_orchestrator.py |
| 3.8 | Update ai_ops_prompts.py (tenant context in prompts) | NOT STARTED | app/services/ai_ops_prompts.py |
| 3.9 | Update ai_ops_knowledge_service.py (per-tenant fix patterns) | NOT STARTED | app/services/ai_ops_knowledge_service.py |
| 3.10 | Update ai_ops_notification_service.py (per-tenant recipients) | NOT STARTED | app/services/ai_ops_notification_service.py |
| 3.11 | Update ai_ops_notes_service.py (tenant scoping) | NOT STARTED | app/services/ai_ops_notes_service.py |

### Phase 4: Worker (Multi-Tenant)

| # | Task | Status | Files |
|---|------|--------|-------|
| 4.1 | Add tenant_context manager to worker.py | NOT STARTED | worker.py |
| 4.2 | Add fair queue polling (round-robin by tenant) | NOT STARTED | worker.py |
| 4.3 | Add usage tracking to pipeline (start/complete/fail) | NOT STARTED | worker.py |
| 4.4 | Add limit checking before pipeline start | NOT STARTED | worker.py |
| 4.5 | Add git pull before agent run | NOT STARTED | worker.py |
| 4.6 | Add git push/PR after FIXED verdict | NOT STARTED | worker.py |
| 4.7 | Update all specialist prompts to use tenant context | NOT STARTED | worker.py |
| 4.8 | Update consensus engine calls to use tenant context | NOT STARTED | app/self_healing/consensus_engine.py |

### Phase 5: Operator Admin Dashboard

| # | Task | Status | Files |
|---|------|--------|-------|
| 5.1 | Create admin base template (dark sidebar, nav) | NOT STARTED | templates/admin/base_admin.html |
| 5.2 | Create admin login page | NOT STARTED | templates/admin/login.html |
| 5.3 | Create admin dashboard (stats, queue, revenue) | NOT STARTED | templates/admin/dashboard.html |
| 5.4 | Create tenants list page | NOT STARTED | templates/admin/tenants.html |
| 5.5 | Create tenant detail page | NOT STARTED | templates/admin/tenant_detail.html |
| 5.6 | Create queue management page | NOT STARTED | templates/admin/queue.html |
| 5.7 | Create billing overview page | NOT STARTED | templates/admin/billing.html |
| 5.8 | Create system health page | NOT STARTED | templates/admin/system.html |
| 5.9 | Create admin settings page | NOT STARTED | templates/admin/settings.html |
| 5.10 | Create admin CSS | NOT STARTED | static/css/admin.css |
| 5.11 | Create admin JS (charts, tables, actions) | NOT STARTED | static/js/admin-dashboard.js |

### Phase 6: Onboarding Wizard

| # | Task | Status | Files |
|---|------|--------|-------|
| 6.1 | Create onboarding base template | NOT STARTED | templates/onboarding/base_onboarding.html |
| 6.2 | Create welcome + plan selection step | NOT STARTED | templates/onboarding/welcome.html |
| 6.3 | Create connect repo step (GitHub App, PAT, SSH key) | NOT STARTED | templates/onboarding/connect_repo.html |
| 6.4 | Create scan codebase step (shows auto-detected context) | NOT STARTED | templates/onboarding/scan_codebase.html |
| 6.5 | Create configure delivery step | NOT STARTED | templates/onboarding/configure_delivery.html |
| 6.6 | Create setup detection step (JS snippet + test) | NOT STARTED | templates/onboarding/setup_detection.html |
| 6.7 | Create onboarding JS (step navigation, API calls) | NOT STARTED | static/js/onboarding.js |
| 6.8 | Create onboarding CSS | NOT STARTED | static/css/onboarding.css |
| 6.9 | Integrate generate_context.py as importable function | NOT STARTED | generate_context.py |

### Phase 7: Tenant Dashboard Additions

| # | Task | Status | Files |
|---|------|--------|-------|
| 7.1 | Create tenant settings page | NOT STARTED | templates/ai_ops/settings.html |
| 7.2 | Create tenant usage page | NOT STARTED | templates/ai_ops/usage.html |
| 7.3 | Create tenant integrations page (snippet, webhooks) | NOT STARTED | templates/ai_ops/integrations.html |
| 7.4 | Update dashboard template (add usage widget) | NOT STARTED | templates/ai_ops/dashboard.html |
| 7.5 | Update session template (show PR link) | NOT STARTED | templates/ai_ops/session.html |
| 7.6 | Update status template (show git operations) | NOT STARTED | templates/ai_ops/status.html |
| 7.7 | Update base template (add settings/usage/integrations nav) | NOT STARTED | templates/ai_ops/base_ai_ops.html |

### Phase 8: Bug Intake (SaaS Mode)

| # | Task | Status | Files |
|---|------|--------|-------|
| 8.1 | Update bug-intake.js (configurable endpoint via window.AI_OPS_CONFIG) | NOT STARTED | static/js/bug-intake.js |
| 8.2 | Add CORS handling for intake endpoint | NOT STARTED | app/__init__.py |
| 8.3 | Create client snippet example | NOT STARTED | examples/client_snippet.example.html |

### Phase 9: Billing Integration

| # | Task | Status | Files |
|---|------|--------|-------|
| 9.1 | Create Valor product/plan setup script | NOT STARTED | scripts/setup_valor_products.py |
| 9.2 | Implement subscription creation via Valor API | NOT STARTED | app/services/billing_service.py |
| 9.3 | Implement overage charging via Valor API | NOT STARTED | app/services/billing_service.py |
| 9.4 | Create signup page with Valor payment form | NOT STARTED | templates/public/signup.html |
| 9.5 | Create pricing page | NOT STARTED | templates/public/pricing.html |
| 9.6 | Handle Valor webhook events | NOT STARTED | app/routes/billing.py |

### Phase 10: Notifications (Multi-Tenant)

| # | Task | Status | Files |
|---|------|--------|-------|
| 10.1 | Create email notification templates | NOT STARTED | templates/notifications/email/*.html |
| 10.2 | Create Slack notification payloads | NOT STARTED | templates/notifications/slack/*.json |
| 10.3 | Implement webhook delivery with HMAC signing | NOT STARTED | app/services/webhook_service.py |
| 10.4 | Update notification service for per-tenant routing | NOT STARTED | app/services/ai_ops_notification_service.py |

### Phase 11: Scripts & Utilities

| # | Task | Status | Files |
|---|------|--------|-------|
| 11.1 | Create operator admin creation script | NOT STARTED | scripts/create_operator_admin.py |
| 11.2 | Create worker health check script | NOT STARTED | scripts/check_worker_health.py |
| 11.3 | Create workspace maintenance cron script | NOT STARTED | scripts/workspace_maintenance.py |
| 11.4 | Create migration runner script | NOT STARTED | scripts/migrate.py |

### Phase 12: Documentation

| # | Task | Status | Files |
|---|------|--------|-------|
| 12.1 | Write README.md | NOT STARTED | README.md |
| 12.2 | Write SETUP_GUIDE.md (operator installation) | NOT STARTED | SETUP_GUIDE.md |
| 12.3 | Write CLIENT_GUIDE.md (what clients see) | NOT STARTED | CLIENT_GUIDE.md |
| 12.4 | Write docs/api_reference.md | NOT STARTED | docs/api_reference.md |
| 12.5 | Write docs/billing.md | NOT STARTED | docs/billing.md |
| 12.6 | Write docs/security.md | NOT STARTED | docs/security.md |
| 12.7 | Update docs/architecture.md for SaaS | NOT STARTED | docs/architecture.md |
| 12.8 | Create .env.example with all SaaS vars | NOT STARTED | .env.example |
| 12.9 | Create examples/nginx.example.conf | NOT STARTED | examples/nginx.example.conf |

### Phase 13: Testing & Verification

| # | Task | Status | Files |
|---|------|--------|-------|
| 13.1 | Verify Flask app starts without errors | NOT STARTED | — |
| 13.2 | Verify worker starts and polls correctly | NOT STARTED | — |
| 13.3 | Test tenant creation + git clone | NOT STARTED | — |
| 13.4 | Test bug intake via API key | NOT STARTED | — |
| 13.5 | Test full agent pipeline for one tenant | NOT STARTED | — |
| 13.6 | Test tenant isolation (no cross-tenant data leaks) | NOT STARTED | — |
| 13.7 | Verify no hardcoded refs (grep scan) | NOT STARTED | — |
| 13.8 | Git init + push to GitHub | NOT STARTED | — |

---

## 23. Task Progress Tracker

**Total tasks:** 98
**Completed:** 62
**In progress:** 0
**Not started:** 36

### Phase Status

| Phase | Tasks | Done | Status |
|-------|-------|------|--------|
| 1. Foundation | 12 | 12 | COMPLETE |
| 2. Auth & Routing | 9 | 9 | COMPLETE |
| 3. Services | 11 | 7 | IN PROGRESS (3.6-3.11 need tenant_id scoping in existing services) |
| 4. Worker | 8 | 0 | NOT STARTED (needs tenant_context manager + fair queue) |
| 5. Admin Dashboard | 11 | 10 | NEARLY COMPLETE (missing admin CSS/JS separate files — inline in templates) |
| 6. Onboarding Wizard | 9 | 9 | COMPLETE |
| 7. Tenant Dashboard | 7 | 1 | IN PROGRESS (base auth updated, templates need settings/usage/integrations pages) |
| 8. Bug Intake | 3 | 1 | IN PROGRESS (CORS done, need JS config update + example) |
| 9. Billing | 6 | 3 | IN PROGRESS (service + webhook handler done, need Valor setup script + signup/pricing pages) |
| 10. Notifications | 4 | 1 | IN PROGRESS (webhook delivery done, need email/Slack templates) |
| 11. Scripts | 4 | 4 | COMPLETE |
| 12. Documentation | 9 | 1 | IN PROGRESS (.env.example done, need README, SETUP_GUIDE, etc.) |
| 13. Testing | 8 | 0 | NOT STARTED |

### Session Log

| Date | Session | What was done |
|------|---------|---------------|
| 2026-03-11 | Initial | Created MASTER_PLAN.md, created repo |
| 2026-03-11 | Build 1 | Phases 1-3 built: 97 files, all migrations (005-011), tenant model, crypto, config, Flask factory, admin routes, API routes, billing routes, onboarding wizard (5 steps), git service, tenant service, billing service (Valor), usage service, webhook service, admin auth, API auth, tenant auth update, 7 admin templates, 6 onboarding templates, 4 scripts. All copied from standalone + new SaaS code. |

---

*This file is the single source of truth for the AI Ops SaaS project. Update task statuses here as work progresses. This file persists across conversation compactions.*
