# Billing

AI Ops uses Valor Payment Systems for subscription billing and overage charges. This document covers plans, usage tracking, and the billing integration.

## Plans and Pricing

| Plan | Monthly Price | Bug Fixes | Features | Overage (Fix) | Overage (Feature) |
|------|--------------|-----------|----------|----------------|-------------------|
| **Trial** | Free (14 days) | 3 | 1 | -- | -- |
| **Starter** | $299/mo | 10 | 2 | $25/fix | $50/feature |
| **Pro** | $799/mo | 30 | 10 | $20/fix | $40/feature |
| **Enterprise** | $1,999/mo | Unlimited | Unlimited | -- | -- |
| **Custom** | Negotiated | SLA, dedicated worker, priority queue | -- | -- | -- |

Plans are configured in Valor and enforced by the usage tracking system. The operator can modify plan limits per tenant in the admin dashboard.

## Usage Tracking

### What Counts as a Bug Fix

A **bug fix** is one full pipeline run: understanding (5 specialists + consolidator) followed by execution (implementer + tester + validator + assessor). If the first attempt produces a PARTIAL or FAILED verdict, automatic retries are free -- they do not count as additional fixes.

Specifically:
- Initial pipeline run = 1 fix counted (when started)
- Auto-retry after PARTIAL/FAILED = free (same session)
- Manual re-queue by the client = 1 new fix counted
- A session that fails all retries still counts as 1 fix

### What Counts as a Feature

A **feature** is a consensus engine run (3-agent debate) plus an implementation pipeline. Planning alone (without approval) does not count. The feature is counted when the client approves the plan and the implementation begins.

### Usage Records

Every pipeline run creates a `usage_record` in the database:

```
usage_record:
  id: UUID
  tenant_id: UUID
  session_id: UUID
  record_type: "bug_fix" | "feature" | "retry"
  status: "started" | "completed" | "failed"
  input_tokens: integer
  output_tokens: integer
  total_cost_cents: integer      # Estimated Claude API cost
  duration_seconds: integer
  agents_used: JSON              # Which agents ran and turn counts
  retries: integer
  verdict: "FIXED" | "PARTIAL" | "FAILED" | null
  billed: boolean
  billed_at: timestamp
```

### Limit Enforcement

Before starting any pipeline run, the worker checks the tenant's usage for the current month:

1. Count completed `bug_fix` records since the 1st of the month
2. Compare against `monthly_fix_limit` for the tenant's plan
3. If over limit, check if the tenant has Valor billing set up for overages
4. If no billing: reject the task and notify the tenant
5. If billing is active: allow the task and queue an overage charge

The same logic applies to features with `monthly_feature_limit`.

### Usage Dashboard

Tenants can view their usage in the dashboard:
- Fixes used vs. limit (e.g., "7/10 fixes this month")
- Features used vs. limit
- Agent time breakdown (total minutes of agent processing)
- Estimated Claude API cost (for the operator's reference)

## Cost Structure

The primary cost per fix is Claude API usage. Estimated costs per pipeline component:

| Component | Turns | Estimated Cost |
|-----------|-------|---------------|
| 5 Specialists (parallel) | 50 each | ~$2.50 total |
| Consolidator | 40 | ~$1.00 |
| Implementer | 150 | ~$8.00 |
| Tester + Validator | 50 + 40 | ~$2.00 |
| Fixer (conditional) | 80 | ~$3.00 |
| Assessor | 40 | ~$1.00 |
| **Total (no retry)** | | **~$14.50** |
| **Total (1 retry)** | | **~$25.50** |
| **Total (2 retries)** | | **~$36.50** |

At Starter pricing ($299/mo for 10 fixes = $29.90/fix), margin is $15.40+ per fix even without retries.

## Valor Payment Systems Integration

### Setup

1. Create a Valor merchant account at valorpaytech.com
2. Run `python scripts/setup_valor_products.py` to create subscription plans
3. Add Valor credentials to `.env`:
   ```
   VALOR_API_BASE=https://api.valorpaytech.com/v1
   VALOR_API_KEY=your-api-key
   VALOR_APP_ID=your-app-id
   VALOR_WEBHOOK_SECRET=your-webhook-secret
   ```

### Customer Lifecycle

When a tenant moves from trial to a paid plan:

1. A Valor customer is created with the tenant's billing email
2. A recurring subscription is created for the selected plan amount
3. The `valor_customer_id` and `valor_subscription_id` are saved on the tenant record

### Overage Charges

When a tenant exceeds their monthly fix or feature limit, a one-time charge is created via the Valor API:

```
POST /v1/charges
{
    "customer_id": "valor_cust_...",
    "amount": 2500,    // $25.00 in cents
    "description": "AI Ops overage: 1x bug_fix"
}
```

### Webhook Events

Valor sends payment events to `https://ops.yourdomain.com/webhooks/valor`. The handler processes these events:

| Event | Action |
|-------|--------|
| `payment.success` | If tenant was suspended, reactivate to `active` |
| `payment.failed` | Set tenant status to `suspended`, notify billing contact |
| `subscription.cancelled` | Set tenant status to `cancelled` |

All webhook payloads are verified via HMAC-SHA256 using the `VALOR_WEBHOOK_SECRET`. Invalid signatures are rejected with 403.

### Webhook Signature Verification

```python
import hmac
import hashlib

def verify_valor_signature(payload_bytes: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)
```

## Without Valor

Valor is optional. If you do not configure Valor credentials:
- Plans and usage limits still work (tracked in the database)
- Overage charges are not automatically billed
- Tenant status changes (suspend, cancel) must be done manually from the admin dashboard
- The `/webhooks/valor` endpoint returns empty responses

You can integrate a different payment processor by modifying `app/services/billing_service.py`.
