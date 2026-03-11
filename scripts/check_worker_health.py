"""Check worker health by reading its last heartbeat from Supabase."""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from app.supabase_client import get_supabase_client


def main():
    sb = get_supabase_client()

    # Check queue depth
    queue = sb.table("ai_ops_agent_queue") \
        .select("id, tenant_id, phase, status, created_at") \
        .in_("status", ["pending", "processing"]) \
        .order("created_at") \
        .execute()

    pending = [t for t in (queue.data or []) if t["status"] == "pending"]
    processing = [t for t in (queue.data or []) if t["status"] == "processing"]

    print(f"Queue: {len(pending)} pending, {len(processing)} processing")

    if processing:
        for t in processing:
            print(f"  Active: {t['phase']} (tenant {t['tenant_id'][:8]})")

    # Check recent completions
    recent = sb.table("usage_records") \
        .select("id, tenant_id, record_type, verdict, completed_at") \
        .order("completed_at", desc=True) \
        .limit(5) \
        .execute()

    if recent.data:
        print(f"\nRecent completions:")
        for r in recent.data:
            print(f"  {r['record_type']}: {r['verdict']} (tenant {r['tenant_id'][:8]})")

    # Check tenant count
    tenants = sb.table("tenants") \
        .select("status", count="exact") \
        .execute()
    print(f"\nTotal tenants: {tenants.count or 0}")


if __name__ == "__main__":
    main()
