# Architecture

## System Overview

AI Ops SaaS is a multi-tenant version of the AI Ops Debugger. One server instance serves multiple client tenants, each with their own codebase, agent pipeline context, and billing. The system has three main components:

1. **Flask Web Application** -- Serves tenant dashboards, operator admin UI, REST API, and bug intake endpoint
2. **Worker Daemon** -- Polls Supabase for queued tasks, loads tenant context, runs Claude agents
3. **Agent Pipeline** -- Teams of Claude Opus agents that investigate, fix, test, and deploy code changes

```
┌──────────────────────────────────────────────────────────────────────┐
│                        OPERATOR'S SERVER                            │
│                                                                     │
│  ┌──────────────┐    ┌───────────────┐    ┌────────────────────┐   │
│  │  Flask Web   │    │   Worker      │    │  Git Workspaces    │   │
│  │  Application │    │   Daemon      │    │                    │   │
│  │              │    │               │    │  /workspaces/      │   │
│  │  - Tenant UI │    │  - Polls      │    │    acme-corp/      │   │
│  │  - Admin UI  │    │    queue      │    │    beta-inc/       │   │
│  │  - REST API  │    │  - Loads      │    │    gamma-llc/      │   │
│  │  - Webhooks  │    │    tenant     │    │                    │   │
│  │  - Intake    │    │    config     │    │  (isolated git     │   │
│  │  - Onboarding│    │  - Runs       │    │   clones, one      │   │
│  │              │    │    agents     │    │   per tenant)      │   │
│  │              │    │  - Pushes     │    │                    │   │
│  │              │    │    fixes      │    │                    │   │
│  └──────┬───────┘    └──────┬───────┘    └────────────────────┘   │
│         │                   │                                      │
│         └───────┬───────────┘                                      │
│                 │                                                   │
│         ┌───────▼────────┐                                         │
│         │   Supabase     │                                         │
│         │   (multi-      │                                         │
│         │    tenant)     │                                         │
│         └────────────────┘                                         │
└──────────────────────────────────────────────────────────────────────┘
         ▲              ▲                    ▲
         │              │                    │
    HTTPS (UI)     HTTPS (API)         Git (SSH/HTTPS)
         │              │                    │
┌────────┴──┐    ┌──────┴─────┐    ┌────────┴────────┐
│ Client's  │    │ Client's   │    │ Client's GitHub  │
│ Browser   │    │ App (bug   │    │ / GitLab repo    │
│ (dashboard│    │  intake JS)│    │                  │
│  login)   │    │            │    │                  │
└───────────┘    └────────────┘    └──────────────────┘
```

## Multi-Tenant Architecture

### Tenant Model

Each tenant represents one client with one codebase. The `tenants` table stores everything about a client: git credentials (encrypted), codebase context, blast radius, notification preferences, billing info, and plan limits.

All other tables (`ai_ops_sessions`, `ai_ops_messages`, `ai_ops_agent_queue`, etc.) include a `tenant_id` column. Every query filters by `tenant_id` to enforce isolation.

### Request Flow -- Bug Report

```
1. Client's app throws a JS error (or user clicks "Report Bug")
2. bug-intake.js captures error + screenshot
3. POST to /api/v1/intake with X-API-Key header
4. Flask validates API key, resolves tenant
5. Creates session + message in Supabase (scoped to tenant_id)
6. Enqueues agent task in ai_ops_agent_queue (with tenant_id)
7. Worker picks up task via fair queue polling
8. Worker loads tenant config from Supabase
9. Worker does git pull in /workspaces/{tenant-slug}/
10. 5 specialist agents analyze the bug (parallel, read-only)
11. Consolidator synthesizes findings
12. Implementer writes the fix
13. Tester + Validator verify (parallel, read-only)
14. Assessor issues verdict (FIXED / PARTIAL / FAILED)
15. If FIXED: git commit + push/PR to tenant's repo
16. Notification sent to tenant (email / Slack / webhook)
17. Usage recorded for billing
```

### Request Flow -- Feature Request

```
1. Client logs into dashboard, describes the feature
2. Consensus engine runs (Architect + Engineer + QA debate)
3. Up to 4 Q&A rounds, 2/3 majority vote required
4. Plan presented to client for approval
5. Client approves -> Implementer builds it
6. Same test/validate/assess cycle as bug fixes
7. Fix delivered as PR to tenant's repo
```

## Worker with Tenant Context

The standalone version reads config from `.env`. The SaaS version keeps `.env` for operator-level settings (Supabase URL, Claude model, server paths) but loads tenant-specific settings from the database before each agent run.

### Context Manager

```python
@contextmanager
def tenant_context(tenant: TenantConfig):
    """Set up environment for a specific tenant's agent run."""
    original_env = os.environ.copy()
    try:
        os.environ["WORKING_DIR"] = tenant.workspace_path
        os.environ["APP_NAME"] = tenant.app_name
        os.environ["APP_BASE_URL"] = tenant.app_url

        # Write tenant's context file to workspace
        context_path = Path(tenant.workspace_path) / ".ai-ops-context.md"
        context_path.write_text(tenant.codebase_context)

        # Write tenant's blast radius to workspace
        if tenant.blast_radius:
            br_path = Path(tenant.workspace_path) / ".ai-ops-blast-radius.json"
            br_path.write_text(json.dumps(tenant.blast_radius))

        yield
    finally:
        os.environ.clear()
        os.environ.update(original_env)
```

### Worker Main Loop

```python
while True:
    task = poll_queue_fair()       # Fair queue: round-robin by tenant
    if task:
        tenant = load_tenant(task.tenant_id)
        if not check_usage_limits(tenant, task.task_type):
            reject_task(task, "Usage limit exceeded")
            continue
        with tenant_context(tenant):
            sync_workspace(tenant)  # git pull
            run_agent_pipeline(task)
    else:
        time.sleep(POLL_INTERVAL)
```

## Git Workspace Management

### Workspace Lifecycle

```
Tenant created -> Clone repo -> Generate context -> Generate manifest -> Ready

                  /srv/ai-ops-saas/workspaces/{tenant-slug}/
                  ├── .git/
                  ├── (entire repo contents)
                  ├── .ai-ops-context.md     (written before each agent run)
                  └── .ai-ops-blast-radius.json
```

### Git Operations

**Initial clone** (tenant onboarding):
```bash
git clone --depth 50 {repo_url} /srv/ai-ops-saas/workspaces/{slug}/
```
Shallow clone (depth 50) saves disk while giving enough history for git blame.

**Pre-agent sync** (before every agent run):
```bash
cd /srv/ai-ops-saas/workspaces/{slug}/
git fetch origin {default_branch}
git reset --hard origin/{default_branch}
git clean -fd
```
Hard reset ensures clean state. Failed agent work from a previous run gets wiped.

**Post-fix delivery** (after FIXED verdict):

Option A -- Pull request (default):
```bash
git checkout -b ai-ops/fix-{session_id}
git add -A
git commit -m "fix: {description} [AI Ops #{session_id}]"
git push origin ai-ops/fix-{session_id}
# Create PR via GitHub/GitLab API
```

Option B -- Direct push:
```bash
git add -A
git commit -m "fix: {description} [AI Ops #{session_id}]"
git push origin {deploy_branch}
```

### Credential Storage

Git credentials are encrypted with Fernet (see [security.md](security.md)). Supported credential types:
- GitHub App installation tokens (recommended)
- Personal access tokens (PAT with repo scope)
- SSH deploy keys (generated per tenant)
- OAuth tokens (via GitHub/GitLab OAuth flow)

### Workspace Maintenance

A daily cron job (`scripts/workspace_maintenance.py`) handles:
1. Prune workspaces for cancelled tenants (after 30-day retention)
2. Run `git gc` on all workspaces to reclaim disk
3. Check disk usage per tenant, alert if >5 GB
4. Verify git remotes are still accessible

## Fair Queue Scheduling

The worker uses fair scheduling to prevent any single tenant from starving others:

```python
def poll_queue_fair() -> Task | None:
    """Pick the next task, rotating between tenants."""
    tasks = supabase.table("ai_ops_agent_queue") \
        .select("*, tenants!inner(status, plan)") \
        .eq("status", "pending") \
        .eq("tenants.status", "active") \
        .order("created_at") \
        .execute()

    if not tasks.data:
        return None

    # Group by tenant, pick the one with the oldest unprocessed task
    # that hasn't been served recently
    return select_fair_task(tasks.data)
```

Enterprise and Custom plan tenants can optionally get priority weighting, but no tenant is ever completely starved.

## Agent Pipeline

The agent pipeline is the same as the standalone version. All intelligence runs on the operator's server -- clients never see agent code, prompts, or intermediate output.

### Understanding Phase

5 specialist agents analyze the bug in parallel:

| Agent | Focus | Mode |
|-------|-------|------|
| Error Analyst | Stack traces, error logs | Read-only, 50 turns, 600s timeout |
| Code Archaeologist | Git history, recent changes | Read-only, 50 turns, 600s timeout |
| Database Inspector | Schema, queries, data | Read-only, 50 turns, 600s timeout |
| UX Flow Mapper | Routes, templates, JS | Read-only, 50 turns, 600s timeout |
| Dependency Auditor | Requirements, imports | Read-only, 50 turns, 600s timeout |

The Consolidator (40 turns, 420s timeout) synthesizes all 5 reports into a technical analysis and user-facing summary.

### Execution Phase

1. **Implementer** (150 turns, 2400s timeout, write access) -- Writes the fix
2. **Regression Tester** + **Supabase Validator** (parallel, read-only) -- Verify the fix
3. **Fixer** (conditional, 80 turns) -- If tests fail, tries a different approach
4. **Browser Smoke Test** (Playwright, deterministic) -- Scripted browser checks
5. **Browser Tester** (Playwright MCP, exploratory) -- Claude-driven browser exploration
6. **Final Assessor** (40 turns) -- Issues verdict: FIXED, PARTIAL, FAILED, or REGRESSION

### Auto-Retry

PARTIAL and FAILED verdicts are re-queued with "try a different approach" instructions. Up to 2 retries. Previous attempt context is passed so agents don't repeat failed approaches.

### Consensus Engine (Features)

For feature requests, 3 Claude Opus agents debate:
- **Architect** -- System design perspective
- **Engineer** -- Implementation feasibility
- **QA Agent** -- Testing and edge cases

Up to 4 Q&A rounds. 2/3 majority vote required to proceed. The approved plan goes to the implementer.

## Data Flow

```
Supabase Tables:
  tenants              -- One row per client (config, billing, git)
  operator_admins      -- Operator admin accounts
  tenant_api_keys      -- API keys per tenant (hashed)
  ai_ops_users         -- Tenant users (scoped by tenant_id)
  ai_ops_sessions      -- One per bug/feature (full lifecycle)
  ai_ops_messages      -- Chat log within each session
  ai_ops_agent_queue   -- Work items for the worker daemon
  ai_ops_tasks         -- Sub-tasks within a session
  ai_ops_files         -- Files modified by agents, screenshots
  ai_ops_fix_patterns  -- Knowledge base (per-tenant)
  ai_ops_notes         -- User feedback and observations
  usage_records        -- Billing usage per agent run
  webhooks             -- Tenant webhook registrations
  audit_log            -- Everything that happened
```

## Infrastructure

### Production Setup (Phase 1: 0-10 tenants)

```
GCP HTTPS Load Balancer (SSL termination)
          |
    ┌─────┴──────┐
    │  Web VM    │     Worker VM
    │ e2-std-2   │     e2-std-4
    │            │
    │  nginx     │     supervisor
    │  gunicorn  │     ai-ops-worker
    │  (4 workers│
    │            │     /srv/ai-ops-saas/
    │            │       workspaces/
    └─────┬──────┘         tenant-a/
          │                tenant-b/
          └────────┐
                   ▼
               Supabase
              (managed)
```

### Scaling

- **Phase 2 (10-50 tenants):** Same web VM. Additional worker VMs, each assigned a subset of tenants.
- **Phase 3 (50+ tenants):** Multiple web VMs behind load balancer. Worker pool with task claiming. Network-attached storage for workspaces.

## Safety Guardrails

- **Blast radius** -- Per-module file allowlist limits what agents can edit
- **Read-only agents** -- Specialists, testers, validators cannot modify files
- **Soak monitoring** -- Post-deploy error log monitoring catches regressions
- **Fix memory** -- Records failed approaches so they aren't repeated (per-tenant)
- **Human approval** -- Feature plans require sign-off before implementation
- **Usage limits** -- Monthly caps prevent runaway costs
- **Auto-retry limits** -- Maximum 2 retries per session
