"""
Daily workspace maintenance cron job.
- Prunes workspaces for cancelled tenants (30-day retention)
- Runs git gc on all workspaces
- Reports disk usage
"""

import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import config
from app.supabase_client import get_supabase_client


def main():
    sb = get_supabase_client()
    workspace_base = Path(config.WORKSPACE_BASE)

    if not workspace_base.exists():
        print(f"Workspace base {workspace_base} does not exist")
        return

    # 1. Prune cancelled tenant workspaces (30-day retention)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cancelled = sb.table("tenants") \
        .select("id, slug, workspace_path") \
        .eq("status", "cancelled") \
        .lt("updated_at", cutoff) \
        .execute()

    for tenant in cancelled.data or []:
        ws = Path(tenant["workspace_path"])
        if ws.exists():
            print(f"Pruning workspace for cancelled tenant: {tenant['slug']}")
            shutil.rmtree(ws, ignore_errors=True)

    # 2. Git gc on active workspaces
    active = sb.table("tenants") \
        .select("slug, workspace_path") \
        .in_("status", ["active", "trial"]) \
        .execute()

    for tenant in active.data or []:
        ws = Path(tenant["workspace_path"])
        if ws.exists() and (ws / ".git").exists():
            subprocess.run(
                ["git", "gc", "--quiet"],
                cwd=str(ws),
                capture_output=True,
                timeout=60,
            )

    # 3. Report disk usage
    total_size = 0
    for d in workspace_base.iterdir():
        if d.is_dir():
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            size_mb = size / (1024 * 1024)
            total_size += size
            if size_mb > 100:
                print(f"  WARNING: {d.name} = {size_mb:.0f} MB")

    print(f"\nTotal workspace disk: {total_size / (1024**3):.1f} GB")


if __name__ == "__main__":
    main()
