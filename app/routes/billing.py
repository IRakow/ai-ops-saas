"""
Billing routes — Valor Payment Systems webhook handler.
"""

import hashlib
import hmac
import logging

from flask import Blueprint, request, jsonify
from app.supabase_client import get_supabase_client
import config

billing_bp = Blueprint("billing", __name__)
logger = logging.getLogger("ai_ops.billing")


@billing_bp.route("/webhooks/valor", methods=["POST"])
def valor_webhook():
    """Handle Valor payment events."""
    payload = request.json or {}
    sig = request.headers.get("X-Valor-Signature", "")

    # Verify HMAC signature
    if config.VALOR_WEBHOOK_SECRET:
        expected = hmac.new(
            config.VALOR_WEBHOOK_SECRET.encode(),
            request.data,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning("Invalid Valor webhook signature")
            return jsonify({"error": "Invalid signature"}), 403

    event_type = payload.get("event")
    customer_id = payload.get("customer_id", "")

    sb = get_supabase_client()

    # Look up tenant by Valor customer ID
    result = sb.table("tenants") \
        .select("id, name, status") \
        .eq("valor_customer_id", customer_id) \
        .maybe_single() \
        .execute()

    if not result.data:
        logger.warning(f"Valor webhook for unknown customer: {customer_id}")
        return "", 200

    tenant = result.data

    if event_type == "subscription.cancelled":
        sb.table("tenants") \
            .update({"status": "cancelled"}) \
            .eq("id", tenant["id"]) \
            .execute()
        logger.info(f"Tenant {tenant['name']} cancelled via Valor webhook")

    elif event_type == "payment.failed":
        sb.table("tenants") \
            .update({"status": "suspended"}) \
            .eq("id", tenant["id"]) \
            .execute()
        logger.warning(f"Payment failed for tenant {tenant['name']}")

        # TODO: Send notification to tenant billing_email

    elif event_type == "payment.success":
        if tenant["status"] == "suspended":
            sb.table("tenants") \
                .update({"status": "active"}) \
                .eq("id", tenant["id"]) \
                .execute()
            logger.info(f"Tenant {tenant['name']} reactivated after payment")

    # Log the event
    sb.table("audit_log").insert({
        "tenant_id": tenant["id"],
        "actor_type": "system",
        "action": f"valor.{event_type}",
        "details": {"payload": payload},
    }).execute()

    return "", 200
