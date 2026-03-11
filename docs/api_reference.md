# API Reference

Base URL: `https://ops.yourdomain.com/api/v1`

All endpoints require an API key in the `X-API-Key` header (or `?api_key=` query parameter). API keys are scoped -- each key has a set of permissions that determine which endpoints it can access.

## Authentication

Every request must include an API key:

```
X-API-Key: aops_live_a1b2c3d4e5f6...
```

API keys are generated in the tenant Settings page. Each key has one or more scopes:

| Scope | Allows |
|-------|--------|
| `intake` | POST /api/v1/intake |
| `read` | GET endpoints (sessions, status) |
| `write` | POST endpoints (approve, reject) |
| `admin` | Webhook management |

A key with `{intake, read}` scopes (the default) can submit bugs and read session data but cannot approve fixes or manage webhooks.

### Error Responses

All errors return JSON:

```json
{"error": "description of what went wrong"}
```

| Status | Meaning |
|--------|---------|
| 401 | Missing or invalid API key |
| 403 | Insufficient scope, or tenant suspended |
| 404 | Resource not found (or belongs to different tenant) |
| 429 | Rate limited (100 intake requests per hour per tenant) |

---

## POST /api/v1/intake

Submit a bug report. This is the endpoint called by the JavaScript snippet and can also be called directly from backend error handlers or CI/CD pipelines.

**Required scope:** `intake`

### Request

```bash
curl -X POST https://ops.yourdomain.com/api/v1/intake \
    -H "Content-Type: application/json" \
    -H "X-API-Key: aops_live_..." \
    -d '{
        "error": "TypeError: Cannot read property '\''id'\'' of undefined",
        "url": "/dashboard/settings",
        "screenshot_base64": "data:image/png;base64,iVBOR...",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "metadata": {
            "user_id": "usr_123",
            "environment": "production",
            "component": "SettingsPanel"
        }
    }'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `error` | string | Yes | Error message or description |
| `url` | string | No | Page URL where the error occurred |
| `screenshot_base64` | string | No | Base64-encoded screenshot (PNG) |
| `user_agent` | string | No | Browser user agent string |
| `metadata` | object | No | Any additional context (user ID, environment, component, etc.) |

### Response

**201 Created**

```json
{
    "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "status": "queued"
}
```

The `session_id` can be used to track the fix progress via the sessions endpoints.

### CORS

This endpoint accepts cross-origin requests. The response includes:

```
Access-Control-Allow-Origin: <request origin>
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: Content-Type, X-API-Key
```

---

## GET /api/v1/sessions

List sessions for your tenant, ordered by newest first.

**Required scope:** `read`

### Request

```bash
curl https://ops.yourdomain.com/api/v1/sessions \
    -H "X-API-Key: aops_live_..."
```

### Response

**200 OK**

```json
[
    {
        "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "session_type": "bug",
        "status": "fixed",
        "title": "TypeError: Cannot read property 'id' of undefined",
        "created_at": "2026-03-11T15:30:00Z"
    },
    {
        "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
        "session_type": "feature",
        "status": "investigating",
        "title": "Add dark mode toggle to settings page",
        "created_at": "2026-03-11T14:20:00Z"
    }
]
```

Returns up to 50 sessions. Sorted by `created_at` descending.

---

## GET /api/v1/sessions/{id}

Get full details for a single session, including all messages from the agent pipeline.

**Required scope:** `read`

### Request

```bash
curl https://ops.yourdomain.com/api/v1/sessions/a1b2c3d4-... \
    -H "X-API-Key: aops_live_..."
```

### Response

**200 OK**

```json
{
    "session": {
        "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "tenant_id": "...",
        "session_type": "bug",
        "status": "fixed",
        "title": "TypeError: Cannot read property 'id' of undefined",
        "verdict": "FIXED",
        "pr_url": "https://github.com/acme/webapp/pull/42",
        "files_changed": ["src/components/Settings.tsx", "src/api/user.ts"],
        "created_at": "2026-03-11T15:30:00Z",
        "updated_at": "2026-03-11T15:45:00Z"
    },
    "messages": [
        {
            "role": "user",
            "content": "**Error:** TypeError: Cannot read property 'id' of undefined\n**URL:** /dashboard/settings",
            "created_at": "2026-03-11T15:30:00Z"
        },
        {
            "role": "assistant",
            "content": "## Investigation Summary\n\nThe error occurs in Settings.tsx when...",
            "created_at": "2026-03-11T15:35:00Z"
        },
        {
            "role": "assistant",
            "content": "## Fix Applied\n\nAdded null check for user.id in...",
            "created_at": "2026-03-11T15:42:00Z"
        }
    ]
}
```

---

## POST /api/v1/sessions/{id}/approve

Approve, reject, or retry a session. Used for feature plans that require human sign-off before implementation.

**Required scope:** `write`

### Request

```bash
curl -X POST https://ops.yourdomain.com/api/v1/sessions/a1b2c3d4-.../approve \
    -H "Content-Type: application/json" \
    -H "X-API-Key: aops_live_..." \
    -d '{"action": "approve"}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | Yes | One of: `approve`, `reject`, `retry` |

### Response

**200 OK**

```json
{"status": "approved"}
```

Actions:
- `approve` -- Moves the session to execution phase. The implementer agent starts writing code.
- `reject` -- Marks the session as rejected. No further work is done.
- `retry` -- Re-queues the session for another attempt with different approach instructions.

---

## GET /api/v1/status

Get your tenant's current status, including queue depth and active agent count.

**Required scope:** `read`

### Request

```bash
curl https://ops.yourdomain.com/api/v1/status \
    -H "X-API-Key: aops_live_..."
```

### Response

**200 OK**

```json
{
    "tenant": "active",
    "plan": "pro",
    "queue_depth": 2,
    "active_agents": 1
}
```

| Field | Type | Description |
|-------|------|-------------|
| `tenant` | string | Tenant status: `active`, `trial`, `suspended`, `cancelled` |
| `plan` | string | Current plan: `starter`, `pro`, `enterprise`, `custom`, `trial` |
| `queue_depth` | integer | Number of pending tasks in the queue |
| `active_agents` | integer | Number of tasks currently being processed |

---

## GET /api/v1/webhooks

List all webhooks registered for your tenant.

**Required scope:** `admin`

### Request

```bash
curl https://ops.yourdomain.com/api/v1/webhooks \
    -H "X-API-Key: aops_live_..."
```

### Response

**200 OK**

```json
[
    {
        "id": "w1b2c3d4-...",
        "url": "https://acme.com/webhooks/ai-ops",
        "events": ["fix.completed", "fix.failed"],
        "is_active": true,
        "last_triggered_at": "2026-03-11T15:45:00Z"
    }
]
```

---

## POST /api/v1/webhooks

Register a new webhook endpoint.

**Required scope:** `admin`

### Request

```bash
curl -X POST https://ops.yourdomain.com/api/v1/webhooks \
    -H "Content-Type: application/json" \
    -H "X-API-Key: aops_live_..." \
    -d '{
        "url": "https://acme.com/webhooks/ai-ops",
        "events": ["fix.completed", "fix.failed", "bug.detected"]
    }'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | HTTPS URL to receive webhook payloads |
| `events` | array | No | List of events to subscribe to (default: `["fix.completed"]`) |

Available events:
- `bug.detected` -- New bug auto-detected by the JS snippet
- `fix.started` -- Agent pipeline started working on a fix
- `fix.completed` -- Fix verified and delivered
- `fix.failed` -- Fix attempt failed after all retries
- `feature.planned` -- Feature plan ready for review
- `feature.completed` -- Feature implemented and delivered
- `deploy.success` -- Code pushed to repo
- `deploy.failed` -- Push/PR creation failed

### Response

**201 Created**

```json
{
    "id": "w1b2c3d4-...",
    "secret": "a1b2c3d4e5f6a7b8c9d0e1f2..."
}
```

Save the `secret` immediately. It is shown once and used to verify webhook signatures. See [docs/security.md](security.md) for HMAC verification details.

---

## DELETE /api/v1/webhooks/{id}

Remove a webhook.

**Required scope:** `admin`

### Request

```bash
curl -X DELETE https://ops.yourdomain.com/api/v1/webhooks/w1b2c3d4-... \
    -H "X-API-Key: aops_live_..."
```

### Response

**204 No Content**

---

## Webhook Payload Format

When an event fires, AI Ops POSTs to your webhook URL:

```json
{
    "event": "fix.completed",
    "timestamp": "2026-03-11T15:45:00Z",
    "tenant_id": "t1b2c3d4-...",
    "data": {
        "session_id": "a1b2c3d4-...",
        "verdict": "FIXED",
        "description": "Fixed null pointer in checkout flow",
        "pr_url": "https://github.com/acme/webapp/pull/42",
        "files_changed": ["src/checkout.py", "src/cart.py"],
        "agent_time_seconds": 340
    }
}
```

The request includes an HMAC-SHA256 signature header:

```
X-AI-Ops-Signature: sha256=abc123def456...
```

Verify it by computing `HMAC-SHA256(webhook_secret, request_body)` and comparing. See [docs/security.md](security.md) for implementation examples.

Webhooks that return non-2xx responses are retried with exponential backoff. After 10 consecutive failures, the webhook is automatically deactivated.
