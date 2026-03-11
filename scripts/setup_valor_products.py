"""Set up Valor Payment Systems products and subscription plans."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import config
import requests

def main():
    if not config.VALOR_API_KEY:
        print("VALOR_API_KEY not set in .env — skipping Valor setup")
        print("You can configure billing later.")
        return

    headers = {
        "Authorization": f"Bearer {config.VALOR_API_KEY}",
        "X-App-Id": config.VALOR_APP_ID,
        "Content-Type": "application/json",
    }

    plans = [
        {"name": "AI Ops Starter", "amount": 29900, "interval": "monthly"},
        {"name": "AI Ops Pro", "amount": 79900, "interval": "monthly"},
        {"name": "AI Ops Enterprise", "amount": 199900, "interval": "monthly"},
    ]

    print("Setting up Valor subscription plans...")
    for plan in plans:
        try:
            resp = requests.post(
                f"{config.VALOR_API_BASE}/plans",
                headers=headers,
                json=plan,
                timeout=30,
            )
            if resp.status_code < 300:
                data = resp.json()
                print(f"  Created: {plan['name']} (${plan['amount']/100:.2f}/mo) — ID: {data.get('id', 'unknown')}")
            else:
                print(f"  Note: {plan['name']} — {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"  Error: {plan['name']} — {e}")

    print("\nDone. Add the plan IDs to your .env if needed.")

if __name__ == "__main__":
    main()
