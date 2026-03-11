"""
Outgoing webhook delivery with HMAC-SHA256 signing.
Delivers events to tenant-registered webhook URLs.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import requests

from app.supabase_client import get_supabase_client

logger = logging.getLogger("ai_ops.webhooks")

MAX_FAILURES = 10
TIMEOUT_SECONDS = 15


def deliver_event(tenant_id: str, event: str, data: dict) -> None:
    """
    Send an event to all matching webhooks for a tenant.
    Runs synchronously — call from worker, not from web requests.
    """
    sb = get_supabase_client()
    hooks = sb.table("webhooks") \
        .select("*") \
        .eq("tenant_id", tenant_id) \
        .eq("is_active", True) \
        .execute()

    for hook in hooks.data or []:
        if event not in hook.get("events", []):
            continue

        if hook.get("failure_count", 0) >= MAX_FAILURES:
            logger.warning(f"Webhook {hook['id']} disabled (too many failures)")
            sb.table("webhooks").update({"is_active": False}).eq("id", hook["id"]).execute()
            continue

        _deliver_single(hook, tenant_id, event, data, sb)


def _deliver_single(hook: dict, tenant_id: str, event: str, data: dict, sb) -> None:
    """Deliver to a single webhook endpoint."""
    payload = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "data": data,
    }

    body = json.dumps(payload, default=str)
    headers = {"Content-Type": "application/json"}

    # Sign with HMAC if secret is configured
    if hook.get("secret"):
        signature = hmac.new(
            hook["secret"].encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers["X-AI-Ops-Signature"] = f"sha256={signature}"

    try:
        resp = requests.post(
            hook["url"],
            data=body,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )

        if resp.status_code < 300:
            sb.table("webhooks").update({
                "last_triggered_at": datetime.now(timezone.utc).isoformat(),
                "failure_count": 0,
            }).eq("id", hook["id"]).execute()
            logger.debug(f"Webhook delivered: {event} → {hook['url']}")
        else:
            _record_failure(hook, sb, f"HTTP {resp.status_code}")

    except requests.RequestException as e:
        _record_failure(hook, sb, str(e))


def _record_failure(hook: dict, sb, reason: str) -> None:
    """Increment failure count on a webhook."""
    new_count = hook.get("failure_count", 0) + 1
    sb.table("webhooks").update({
        "failure_count": new_count,
    }).eq("id", hook["id"]).execute()
    logger.warning(f"Webhook {hook['id']} failed ({new_count}): {reason}")
