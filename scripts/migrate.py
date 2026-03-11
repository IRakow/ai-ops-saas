"""Run all SQL migrations in order against Supabase."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from app.supabase_client import get_supabase_client


def main():
    migrations_dir = Path(__file__).parent.parent / "migrations"
    sql_files = sorted(migrations_dir.glob("*.sql"))

    if not sql_files:
        print("No migration files found")
        return

    sb = get_supabase_client()

    for f in sql_files:
        print(f"Running {f.name}...", end=" ")
        sql = f.read_text()
        try:
            sb.postgrest.session.headers.update({"Prefer": "return=minimal"})
            sb.rpc("exec_sql", {"query": sql}).execute()
            print("OK")
        except Exception as e:
            print(f"NOTE: {e}")
            print(f"  → Run this migration manually in the Supabase SQL Editor")

    print("\nDone. If any migrations failed, run them via the Supabase SQL Editor.")


if __name__ == "__main__":
    main()
