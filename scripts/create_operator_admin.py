"""Create an operator admin account for the SaaS dashboard."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import bcrypt
from app.supabase_client import get_supabase_client


def main():
    parser = argparse.ArgumentParser(description="Create operator admin")
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--super-admin", action="store_true")
    args = parser.parse_args()

    sb = get_supabase_client()
    pw_hash = bcrypt.hashpw(args.password.encode(), bcrypt.gensalt()).decode()

    sb.table("operator_admins").insert({
        "name": args.name,
        "email": args.email.lower(),
        "password_hash": pw_hash,
        "is_super_admin": args.super_admin,
    }).execute()

    print(f"Operator admin created: {args.email}")


if __name__ == "__main__":
    main()
