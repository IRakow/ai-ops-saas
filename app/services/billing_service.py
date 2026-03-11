"""
Valor Payment Systems integration.
Handles subscription creation, overage charges, and customer management.
"""

import logging

import requests

from app.supabase_client import get_supabase_client
from app.tenant import load_tenant
import config

logger = logging.getLogger("ai_ops.billing")

PLAN_AMOUNTS = {
    "starter": 29900,      # $299.00 in cents
    "pro": 79900,           # $799.00
    "enterprise": 199900,   # $1,999.00
}

OVERAGE_AMOUNTS = {
    "bug_fix": 2500,    # $25.00
    "feature": 5000,    # $50.00
}


def _valor_headers() -> dict:
    return {
        "Authorization": f"Bearer {config.VALOR_API_KEY}",
        "X-App-Id": config.VALOR_APP_ID,
        "Content-Type": "application/json",
    }


def create_subscription(tenant_id: str, plan: str, billing_email: str) -> dict:
    """Create a Valor customer and recurring subscription."""
    tenant = load_tenant(tenant_id)

    if not config.VALOR_API_KEY:
        logger.warning("Valor API key not configured, skipping billing setup")
        return {}

    # Create customer
    customer_resp = requests.post(
        f"{config.VALOR_API_BASE}/customers",
        headers=_valor_headers(),
        json={
            "email": billing_email,
            "description": f"AI Ops tenant: {tenant.name}",
        },
        timeout=30,
    )
    customer_resp.raise_for_status()
    customer = customer_resp.json()

    # Create subscription
    amount = PLAN_AMOUNTS.get(plan, PLAN_AMOUNTS["starter"])
    sub_resp = requests.post(
        f"{config.VALOR_API_BASE}/subscriptions",
        headers=_valor_headers(),
        json={
            "customer_id": customer["id"],
            "amount": amount,
            "interval": "monthly",
            "trial_days": config.TRIAL_DAYS,
            "description": f"AI Ops {plan.title()} Plan",
        },
        timeout=30,
    )
    sub_resp.raise_for_status()
    subscription = sub_resp.json()

    # Update tenant
    sb = get_supabase_client()
    sb.table("tenants").update({
        "valor_customer_id": customer["id"],
        "valor_subscription_id": subscription["id"],
        "billing_email": billing_email,
        "plan": plan,
        "monthly_fix_limit": {"starter": 10, "pro": 30, "enterprise": 9999}.get(plan, 10),
        "monthly_feature_limit": {"starter": 2, "pro": 10, "enterprise": 9999}.get(plan, 2),
    }).eq("id", tenant_id).execute()

    from app.tenant import invalidate_tenant_cache
    invalidate_tenant_cache(tenant_id)

    logger.info(f"Created Valor subscription for tenant {tenant.name} ({plan})")
    return {"customer_id": customer["id"], "subscription_id": subscription["id"]}


def charge_overage(tenant_id: str, task_type: str, count: int = 1) -> bool:
    """Charge an overage fee for usage beyond plan limits."""
    tenant = load_tenant(tenant_id)

    if not config.VALOR_API_KEY or not tenant.valor_customer_id:
        logger.warning(f"Cannot charge overage: no Valor setup for tenant {tenant_id}")
        return False

    amount = OVERAGE_AMOUNTS.get(task_type, 2500) * count

    try:
        resp = requests.post(
            f"{config.VALOR_API_BASE}/charges",
            headers=_valor_headers(),
            json={
                "customer_id": tenant.valor_customer_id,
                "amount": amount,
                "description": f"AI Ops overage: {count}x {task_type}",
            },
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"Charged ${amount/100:.2f} overage to tenant {tenant.name}")
        return True
    except Exception as e:
        logger.error(f"Overage charge failed: {e}")
        return False


def cancel_subscription(tenant_id: str) -> bool:
    """Cancel a tenant's subscription."""
    tenant = load_tenant(tenant_id)

    if not config.VALOR_API_KEY or not tenant.valor_subscription_id:
        return False

    try:
        resp = requests.delete(
            f"{config.VALOR_API_BASE}/subscriptions/{tenant.valor_subscription_id}",
            headers=_valor_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"Cancelled subscription for tenant {tenant.name}")
        return True
    except Exception as e:
        logger.error(f"Subscription cancellation failed: {e}")
        return False
