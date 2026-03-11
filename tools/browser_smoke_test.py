#!/usr/bin/env python3
"""
browser_smoke_test.py — Deterministic Playwright browser smoke tests.

Tests critical user flows: login, dashboard, properties, tenants, maintenance,
leasing, calendar, accounting. Each page is loaded, checked for errors, and
screenshotted on failure.

Output: "BROWSER SMOKE TEST PASS" or "BROWSER SMOKE TEST FAIL: <details>"

Usage:
    python3 browser_smoke_test.py [--base-url http://127.0.0.1:8000]
"""

import argparse
import os
import sys
import time
from pathlib import Path

SCREENSHOT_DIR = Path("/tmp/browser_smoke_screenshots")
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
LOGIN_EMAIL = os.getenv("SOAK_CHECK_EMAIL", "")
LOGIN_PASSWORD = os.getenv("SOAK_CHECK_PASSWORD", "")
TOTAL_TIMEOUT = 90  # seconds


def run_smoke_tests(base_url: str) -> tuple[bool, str]:
    """Run all browser smoke tests. Returns (passed, details)."""
    from playwright.sync_api import sync_playwright

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    failures = []
    js_errors = []
    start = time.monotonic()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = context.new_page()

        # Collect JS console errors
        page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

        # ---- Login Flow ----
        try:
            page.goto(f"{base_url}/login", wait_until="networkidle", timeout=20000)
            page.wait_for_selector('input[type="email"], input[name="email"], input[type="text"]', timeout=10000)
            email_input = page.locator('input[type="email"], input[name="email"], input[type="text"]').first
            email_input.fill(LOGIN_EMAIL)
            password_input = page.locator('input[type="password"]').first
            password_input.fill(LOGIN_PASSWORD)
            submit = page.locator('button[type="submit"], button:has-text("Sign In"), button:has-text("Log In")').first
            submit.click()
            # Wait for navigation away from /login (could go to /dashboard, /sales-tool, etc.)
            page.wait_for_function(
                "() => !window.location.pathname.includes('/login')",
                timeout=15000,
            )
        except Exception as e:
            failures.append(f"Login flow: {e}")
            page.screenshot(path=str(SCREENSHOT_DIR / "login_fail.png"))

        # ---- Pages to check (after login) ----
        # These are the core property management pages; login redirects
        # depend on user role but all users can access these via direct nav
        pages_to_check = [
            ("Dashboard", "/dashboard"),
            ("Properties", "/properties"),
            ("Tenants", "/tenants"),
            ("Maintenance", "/maintenance"),
            ("Leasing", "/leasing"),
            ("Calendar", "/calendar"),
            ("Accounting", "/accounting"),
        ]

        for name, path in pages_to_check:
            if time.monotonic() - start > TOTAL_TIMEOUT:
                failures.append(f"Timed out ({TOTAL_TIMEOUT}s) before checking {name}")
                break

            try:
                resp = page.goto(f"{base_url}{path}", wait_until="networkidle", timeout=15000)
                status = resp.status if resp else 0

                if status >= 500:
                    failures.append(f"{name} ({path}): HTTP {status}")
                    page.screenshot(path=str(SCREENSHOT_DIR / f"{name.lower()}_fail.png"))
                    continue

                # Check for error content in page
                content = page.content()
                error_markers = ["Internal Server Error", "500 Error", "Traceback (most recent call last)"]
                for marker in error_markers:
                    if marker in content:
                        failures.append(f"{name} ({path}): page contains '{marker}'")
                        page.screenshot(path=str(SCREENSHOT_DIR / f"{name.lower()}_fail.png"))
                        break

            except Exception as e:
                failures.append(f"{name} ({path}): {e}")
                try:
                    page.screenshot(path=str(SCREENSHOT_DIR / f"{name.lower()}_fail.png"))
                except Exception:
                    pass

        browser.close()

    elapsed = int(time.monotonic() - start)

    # Filter JS errors (ignore common noise)
    real_js_errors = [
        e for e in js_errors
        if not any(skip in e for skip in [
            "favicon", "ResizeObserver", "third-party", "analytics",
            "Failed to load resource", "404", "net::ERR",
        ])
    ]

    if failures:
        detail = "; ".join(failures[:5])
        return False, f"BROWSER SMOKE TEST FAIL: {detail} ({elapsed}s)"

    if real_js_errors:
        detail = "; ".join(real_js_errors[:3])
        return True, f"BROWSER SMOKE TEST PASS (with JS warnings: {detail}) ({elapsed}s)"

    return True, f"BROWSER SMOKE TEST PASS ({len(pages_to_check)} pages OK, {elapsed}s)"


def main():
    parser = argparse.ArgumentParser(description="Browser smoke tests")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    passed, details = run_smoke_tests(args.base_url)
    print(details)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
