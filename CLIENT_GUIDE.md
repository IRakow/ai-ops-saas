# AI Ops -- Client Guide

This guide explains how to set up AI Ops on your application and get automatic bug detection and fixing.

## Getting Started

### 1. Sign Up

Go to your operator's AI Ops URL (e.g., `https://ops.example.com/signup`) and create an account. You will start with a 14-day free trial.

### 2. Connect Your Repository

During onboarding, connect your GitHub (or GitLab/Bitbucket) repository. You have three options:

- **GitHub App** (recommended) -- One-click install. Grants AI Ops read/write access to your repo without sharing personal tokens.
- **Personal Access Token** -- Generate a token with `repo` scope at github.com/settings/tokens and paste it in.
- **Deploy Key** -- AI Ops generates an SSH key pair. Add the public key to your repo's deploy keys with write access.

AI Ops clones your repo and scans the codebase to understand your stack, file structure, and patterns. You can review and edit the generated description before proceeding.

### 3. Choose Fix Delivery Method

Select how you want fixes delivered to your repo:

- **Pull Request** (default, recommended) -- AI Ops creates a PR for each fix. You review and merge at your pace.
- **Direct Push** -- Fixes are pushed directly to your deploy branch. Faster, but no review step.
- **Webhook Only** -- AI Ops notifies you of the fix but does not push. You apply it manually.

### 4. Add Bug Detection

Paste this snippet into your base HTML template, before the closing `</body>` tag:

```html
<link rel="stylesheet" href="https://ops.example.com/static/css/bug-intake.css">
<script>
  window.AI_OPS_CONFIG = {
    endpoint: "https://ops.example.com/api/v1/intake",
    apiKey: "YOUR_API_KEY_HERE"
  };
</script>
<script src="https://ops.example.com/static/js/bug-intake.js"></script>
```

Replace `ops.example.com` with your operator's domain and `YOUR_API_KEY_HERE` with the API key from your dashboard Settings page.

### 5. Test It

Click the floating bug icon on any page of your app and submit a test report. It should appear in your AI Ops dashboard within 30 seconds.

---

## Dashboard Overview

### Sessions

Every bug report and feature request creates a **session**. A session tracks the full lifecycle from detection through investigation, fixing, testing, and deployment.

Session statuses:
- **New** -- Just received, waiting in queue
- **Investigating** -- Specialist agents are analyzing the bug
- **Understood** -- Analysis complete, preparing fix
- **Fixing** -- Implementer agent is writing the fix
- **Testing** -- Tester and validator agents are verifying
- **Fixed** -- Fix verified and delivered (PR created or code pushed)
- **Partial** -- Improvement made but not fully resolved (auto-retries)
- **Failed** -- Could not fix after all attempts

### Status Page

Shows real-time progress of any active agent work. You can see which phase the pipeline is in and read agent output as it happens.

### History

All past sessions with their outcomes. Filter by status, date, or type (bug vs. feature).

### Settings

Manage your configuration:
- **Repository** -- Change repo URL, re-authenticate, switch branches
- **Team Members** -- Add or remove users who can access the dashboard
- **Notifications** -- Configure email, Slack, or webhook notifications
- **API Keys** -- Generate new keys, revoke old ones
- **Webhooks** -- Register URLs to receive event notifications
- **Delivery Method** -- Change how fixes are sent to your repo
- **Codebase Context** -- Edit the auto-generated description that agents use to understand your app
- **Blast Radius** -- Configure which files agents are allowed to modify

---

## How Bug Detection Works

The JavaScript snippet does four things:

1. **Intercepts HTTP errors** -- Monitors `fetch()` and `XMLHttpRequest` responses. Any 500+ status code is captured.
2. **Catches JavaScript errors** -- Listens for `window.onerror` and `unhandledrejection` events.
3. **Takes screenshots** -- Uses html2canvas to capture the current page state when an error occurs.
4. **Manual reporting** -- Adds a floating button so your users (or your team) can manually report bugs with a description.

Captured errors are sent to the AI Ops API along with the page URL, user agent, and any metadata you configure. From there, the agent pipeline takes over.

---

## Requesting Features

From the dashboard, click **"Request Feature"** (or equivalent button your operator has configured). Describe what you want in plain language. The system runs a consensus engine where three AI agents (Architect, Engineer, QA) debate the best approach, then present you with a plan for approval.

After you approve the plan, the implementation pipeline runs the same way as a bug fix: implement, test, validate, deliver.

---

## API Access

For programmatic integration (CI/CD pipelines, custom error handlers, backend services), use the REST API directly.

### Report a Bug

```bash
curl -X POST https://ops.example.com/api/v1/intake \
    -H "Content-Type: application/json" \
    -H "X-API-Key: aops_live_..." \
    -d '{
        "error": "TypeError: Cannot read property '\''id'\'' of undefined",
        "url": "/dashboard",
        "user_agent": "Mozilla/5.0...",
        "metadata": {"user_id": "123", "environment": "production"}
    }'
```

Response:
```json
{"session_id": "a1b2c3d4-...", "status": "queued"}
```

### List Sessions

```bash
curl https://ops.example.com/api/v1/sessions \
    -H "X-API-Key: aops_live_..."
```

### Get Session Detail

```bash
curl https://ops.example.com/api/v1/sessions/a1b2c3d4-... \
    -H "X-API-Key: aops_live_..."
```

### Approve a Fix Plan

```bash
curl -X POST https://ops.example.com/api/v1/sessions/a1b2c3d4-.../approve \
    -H "Content-Type: application/json" \
    -H "X-API-Key: aops_live_..." \
    -d '{"action": "approve"}'
```

### Check Status

```bash
curl https://ops.example.com/api/v1/status \
    -H "X-API-Key: aops_live_..."
```

Response:
```json
{
    "tenant": "active",
    "plan": "pro",
    "queue_depth": 2,
    "active_agents": 1
}
```

See [docs/api_reference.md](docs/api_reference.md) for the full API documentation with all endpoints, request/response schemas, and error codes.

---

## FAQ

**How long does a fix take?**

A typical bug fix takes 5-15 minutes from detection to pull request. Complex multi-file fixes or features can take 20-40 minutes. If the first attempt does not fully resolve the issue, the system retries up to 2 times with different approaches.

**Can AI Ops break my app?**

By default, fixes are delivered as pull requests. You review and merge them yourself. If you choose direct push, the system runs automated tests (regression tester + validator + browser smoke tests) before pushing. You can also configure a blast radius to limit which files agents can modify.

**What languages/frameworks does it support?**

AI Ops works with any codebase that Claude can read. It has been tested extensively with Python (Flask, Django), JavaScript/TypeScript (React, Next.js, Node.js), and Ruby on Rails. It handles frontend and backend bugs, database issues, dependency conflicts, and UI flow breakdowns.

**Does AI Ops store my code?**

Your code is cloned into an isolated workspace on the operator's server. Each tenant has a separate directory with its own git clone. Workspaces are encrypted at rest on the server's filesystem. Code is never shared between tenants or exposed through the API.

**What happens when I hit my monthly limit?**

You will receive a notification when you approach your monthly fix/feature limit. If you exceed it, additional fixes are charged at the overage rate for your plan. You can upgrade your plan at any time.

**Can I use this with private repos?**

Yes. All three authentication methods (GitHub App, PAT, deploy key) work with private repositories.

**How do I cancel?**

Go to Settings in your dashboard or contact your operator. Your workspace and data are retained for 30 days after cancellation, then permanently deleted.
