# Security Model

This document describes how AI Ops SaaS protects tenant data, credentials, and agent operations.

## Tenant Isolation

Every tenant's data is isolated through multiple layers:

### Database Isolation

All tenant-scoped tables include a `tenant_id` column. Every query filters by `tenant_id`:

```python
# Every query looks like this -- no exceptions
sessions = sb.table("ai_ops_sessions") \
    .select("*") \
    .eq("tenant_id", current_tenant_id) \
    .execute()
```

The `tenant_id` comes from the authenticated session (for web requests) or the API key (for API requests). There is no way to query another tenant's data through the application.

Supabase Row-Level Security (RLS) is enabled on all tenant-scoped tables as defense-in-depth. The service role key used by the worker bypasses RLS, but the application always includes `tenant_id` filters.

### Filesystem Isolation

Each tenant gets a separate git workspace directory:

```
/srv/ai-ops-saas/workspaces/
    acme-corp/          # Tenant A's code
    beta-inc/           # Tenant B's code
    gamma-llc/          # Tenant C's code
```

Agent subprocesses receive `WORKING_DIR` set to the tenant's workspace. The Claude Code CLI operates within that directory. Workspaces are owned by the worker process user with 700 permissions.

### Git Isolation

Each tenant's git credentials are stored separately, encrypted at rest. Git operations use tenant-specific credentials. There is no shared git configuration between tenants.

### Agent Context Isolation

Each agent run loads the tenant's own:
- `codebase_context` -- Description of their app for agent prompts
- `blast_radius` -- Which files agents can modify
- `agent_protocol` -- Custom rules and constraints
- `manifest` -- Cached codebase structure

No context from one tenant is ever injected into another tenant's agent prompts.

### Fix Memory Isolation

The `ai_ops_fix_patterns` table includes `tenant_id`. Each tenant builds their own knowledge base of what fixes worked and what failed. Patterns from Tenant A are never used for Tenant B.

## Credential Encryption

### Git Credentials (Fernet)

Git tokens, deploy keys, and personal access tokens are encrypted at rest using Fernet symmetric encryption (AES-128-CBC with HMAC-SHA256 authentication).

The encryption key is derived from the operator's `SECRET_KEY`:

```python
import base64
import hashlib
from cryptography.fernet import Fernet

def _get_fernet(secret_key: str) -> Fernet:
    key = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))
```

Credentials are decrypted only in memory, immediately before a git operation, and never written to disk in plaintext.

If you rotate `SECRET_KEY`, all stored git credentials become unreadable. You would need to re-encrypt them or have tenants re-authenticate.

### Session Data

Flask sessions use signed cookies. The cookie is signed with `SECRET_KEY` using HMAC. The tenant ID stored in the session cannot be tampered with without invalidating the signature.

## API Key Management

### Key Format

API keys follow the format: `aops_live_{48_hex_characters}`

Example: `aops_live_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4`

### Storage

Keys are never stored in plaintext. On creation:
1. The full key is shown to the user exactly once
2. A SHA-256 hash is computed and stored in `tenant_api_keys.key_hash`
3. The first 16 characters are stored in `key_prefix` for display (e.g., `aops_live_a1b2c3`)

### Scopes

Each key has a set of scopes that limit what it can do:

| Scope | Endpoints |
|-------|-----------|
| `intake` | POST /api/v1/intake |
| `read` | GET /api/v1/sessions, GET /api/v1/status |
| `write` | POST /api/v1/sessions/{id}/approve |
| `admin` | Webhook CRUD |

Default scopes for a new key: `{intake, read}`.

### Key Rotation

Tenants can generate new keys and revoke old ones from the Settings page. Revoking a key sets `is_active = false` in the database. The old key immediately stops working.

Keys can have an optional `expires_at` date. Expired keys are rejected during authentication.

### Lookup Performance

API key lookup uses the SHA-256 hash with a database index (`idx_api_keys_hash`). The lookup is O(1) against the index.

## CORS Policy

Cross-origin requests are only allowed on the intake endpoint (`/api/v1/intake`):

```
Access-Control-Allow-Origin: <request origin>
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: Content-Type, X-API-Key
```

All other endpoints reject cross-origin requests. The intake endpoint accepts any origin because the JavaScript snippet runs on the client's domain, which varies per tenant.

The API key in the `X-API-Key` header acts as the authorization layer. Even if a malicious site makes cross-origin requests, they cannot submit bugs without a valid API key.

## Webhook HMAC Signing

### Outgoing Webhooks (to tenants)

Every outgoing webhook payload is signed with HMAC-SHA256:

```python
import hmac
import hashlib

signature = hmac.new(
    webhook_secret.encode(),
    payload_body.encode(),
    hashlib.sha256,
).hexdigest()

# Sent as header:
# X-AI-Ops-Signature: sha256=<signature>
```

Tenants should verify this signature before processing the payload.

### Verification Example (Python)

```python
import hmac
import hashlib

def verify_webhook(request_body: bytes, signature_header: str, secret: str) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    received_sig = signature_header[7:]  # Strip "sha256=" prefix
    expected = hmac.new(
        secret.encode(),
        request_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(received_sig, expected)
```

### Verification Example (Node.js)

```javascript
const crypto = require('crypto');

function verifyWebhook(body, signatureHeader, secret) {
    if (!signatureHeader?.startsWith('sha256=')) return false;
    const receivedSig = signatureHeader.slice(7);
    const expected = crypto
        .createHmac('sha256', secret)
        .update(body)
        .digest('hex');
    return crypto.timingSafeEqual(
        Buffer.from(receivedSig),
        Buffer.from(expected)
    );
}
```

### Incoming Webhooks (from Valor)

Valor payment webhooks are verified the same way, using the `VALOR_WEBHOOK_SECRET` and the `X-Valor-Signature` header.

### Auto-Deactivation

Webhooks that fail 10 consecutive times are automatically deactivated (`is_active = false`). This prevents the system from repeatedly calling dead endpoints. Tenants can reactivate webhooks from the Settings page.

## Audit Logging

Every significant action is logged in the `audit_log` table:

```sql
audit_log:
    id UUID
    tenant_id UUID          -- NULL for operator-level actions
    actor_type VARCHAR(20)  -- 'operator', 'tenant_admin', 'tenant_user', 'api_key', 'system'
    actor_id UUID
    action VARCHAR(100)     -- 'session.created', 'fix.approved', 'git.pushed', etc.
    resource_type VARCHAR(50)
    resource_id UUID
    details JSONB           -- Context-specific data
    ip_address INET
    created_at TIMESTAMPTZ
```

Actions logged include:
- Session creation and status changes
- Fix approvals and rejections
- Git operations (clone, pull, push, PR creation)
- API key creation and revocation
- Webhook registration and deletion
- Tenant status changes (activate, suspend, cancel)
- Operator admin actions (impersonation, config changes)
- Payment events (via Valor webhooks)
- Login attempts (successful and failed)

Audit logs are retained indefinitely. They are queryable by tenant, actor, action, and date range from the operator admin dashboard.

## Access Control Matrix

| Action | Operator Admin | Tenant Admin | Tenant User | API Key |
|--------|:-:|:-:|:-:|:-:|
| View all tenants | Yes | | | |
| Create/delete tenants | Yes | | | |
| Manage tenant settings | Yes | Own tenant | | |
| Manage team members | Yes | Own tenant | | |
| View sessions | Yes | Own tenant | Own tenant | `read` scope |
| Report bugs | Yes | Own tenant | Own tenant | `intake` scope |
| Approve/reject fixes | Yes | Own tenant | Own tenant | `write` scope |
| View billing | Yes | Own tenant | | |
| Manage API keys | Yes | Own tenant | | `admin` scope |
| Manage webhooks | Yes | Own tenant | | `admin` scope |
| System settings | Yes | | | |
| Impersonate tenant | Yes | | | |

## Agent Sandboxing

- Agents run exclusively in the tenant's git workspace directory
- Blast radius config limits which files each agent can modify per module
- Read-only agents (specialists, tester, validator, assessor) cannot modify files
- Only the implementer and fixer agents have write access
- All agent subprocess calls use `stdin=subprocess.DEVNULL` to prevent interactive prompts

## Network Security Recommendations

### HTTPS

Terminate TLS at the load balancer (GCP HTTPS LB or Cloudflare). The Flask app and nginx on the VM handle HTTP only. All client-facing traffic should be HTTPS.

### Firewall

- Web VM: Allow inbound on port 80 (from load balancer only) and 22 (SSH)
- Worker VM: Allow inbound on port 22 (SSH) only. No public web traffic.
- Both VMs: Allow outbound to Supabase, GitHub/GitLab, Claude API, Valor API, SendGrid

### Rate Limiting

Rate limiting is applied at two layers:

1. **nginx** -- Connection-level limiting on the intake endpoint (see `examples/nginx.example.conf`)
2. **Application** -- Per-tenant limits: 100 intake requests per hour, 20 API requests per minute

### Secret Rotation

| Secret | Rotation Impact |
|--------|----------------|
| `SECRET_KEY` | Invalidates all Flask sessions (users must re-login) and breaks git credential decryption (must re-encrypt) |
| `SUPABASE_SERVICE_ROLE_KEY` | Rotate in Supabase dashboard, update `.env` on both VMs |
| API keys | Tenants generate new keys and revoke old ones from Settings |
| Webhook secrets | Delete and re-create the webhook to get a new secret |
| `VALOR_WEBHOOK_SECRET` | Update in both Valor portal and `.env` |

### Production Hardening Checklist

- [ ] `SECRET_KEY` set to a random 64+ character hex string (not the default)
- [ ] `FLASK_ENV=production` (not `development`)
- [ ] Supabase using service role key (not anon key)
- [ ] HTTPS enforced on all endpoints
- [ ] Firewall rules restricting VM access
- [ ] Supervisor running processes as non-root users (`www-data` for web, `ai-ops` for worker)
- [ ] Log directory permissions restricted (700)
- [ ] `.env` file permissions restricted (600)
- [ ] No `.env` in git (check `.gitignore`)
- [ ] Workspace directory permissions restricted (700, owned by worker user)
