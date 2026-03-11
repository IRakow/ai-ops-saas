"""
AI Ops Debugger
---------------
Regression smoke tests for the deployed application.
Run after every AI-driven deploy to verify the app still works.

Usage:
    python ai_ops_smoke_test.py
    python ai_ops_smoke_test.py --base-url http://localhost:8000

Environment variables:
    SMOKE_TEST_EMAIL    - Login email
    SMOKE_TEST_PASSWORD - Login password
"""

import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone

import config

# ---------------------------------------------------------------------------
# HTTP client: prefer requests if installed, fall back to stdlib urllib
# ---------------------------------------------------------------------------
try:
    import requests as _requests_lib

    _USE_REQUESTS = True
except ImportError:
    _USE_REQUESTS = False
    import urllib.request
    import urllib.error


# ---------------------------------------------------------------------------
# Friendly display names for non-technical users
# ---------------------------------------------------------------------------
_FRIENDLY_NAMES = {
    "/health": "App Health Check",
    "/api/auth/login": "Login System",
    "/api/dashboard/metrics": "Dashboard",
    "/api/properties": "Properties",
    "/accounting/api/tenants": "Tenants",
    "/api/maintenance/work-orders": "Maintenance Work Orders",
    "/leasing/api/applications": "Leasing Applications",
    "/accounting/api/reports/chart-of-accounts": "Chart of Accounts",
    "/calendar/api/events": "Calendar",
}


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _http_request_requests(url, method="GET", headers=None, body=None, timeout=10):
    """Make an HTTP request using the ``requests`` library."""
    headers = headers or {}
    try:
        if method == "POST":
            resp = _requests_lib.request(
                method, url, headers=headers, json=body, timeout=timeout
            )
        else:
            resp = _requests_lib.request(
                method, url, headers=headers, timeout=timeout
            )
        body_text = resp.text[:200] if resp.text else ""
        return resp.status_code, body_text, None
    except _requests_lib.exceptions.Timeout:
        return None, None, "The server took too long to respond"
    except _requests_lib.exceptions.ConnectionError:
        return None, None, "Could not connect to the server"
    except Exception:
        return None, None, "An unexpected issue occurred while checking this page"


def _http_request_urllib(url, method="GET", headers=None, body=None, timeout=10):
    """Make an HTTP request using only ``urllib`` from the standard library."""
    headers = headers or {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    # Allow self-signed certs on localhost / internal VMs
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")[:200]
            return resp.status, body_text, None
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return exc.code, body_text, None
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if exc.reason else str(exc)
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            return None, None, "The server took too long to respond"
        return None, None, "Could not connect to the server"
    except Exception:
        return None, None, "An unexpected issue occurred while checking this page"


def _http(url, method="GET", headers=None, body=None, timeout=10):
    """Dispatch to whichever HTTP backend is available."""
    if _USE_REQUESTS:
        return _http_request_requests(url, method, headers, body, timeout)
    return _http_request_urllib(url, method, headers, body, timeout)


# ---------------------------------------------------------------------------
# Individual endpoint tests
# ---------------------------------------------------------------------------

def _test_endpoint(base_url, endpoint, method="GET", headers=None, body=None,
                   timeout=10, check_fn=None):
    """
    Test a single endpoint.

    Returns a result dict with keys:
        endpoint, method, status, passed, response_time_ms, error, body_preview
    """
    url = base_url.rstrip("/") + endpoint
    start = time.monotonic()
    status, body_text, error = _http(url, method=method, headers=headers,
                                     body=body, timeout=timeout)
    elapsed_ms = round((time.monotonic() - start) * 1000)

    passed = False
    if error is None and status is not None:
        passed = 200 <= status < 300
        # Allow custom validation on top of status check
        if passed and check_fn is not None:
            try:
                passed = check_fn(status, body_text)
            except Exception:
                passed = False

    friendly = _FRIENDLY_NAMES.get(endpoint, endpoint)
    if error:
        display_error = error
    elif not passed:
        # Translate HTTP codes into plain English
        if status == 401:
            display_error = "{} requires login credentials".format(friendly)
        elif status == 403:
            display_error = "{} access was denied".format(friendly)
        elif status == 404:
            display_error = "{} page was not found".format(friendly)
        elif status == 500:
            display_error = "{} encountered a server error".format(friendly)
        elif status is not None:
            display_error = "{} is not responding as expected".format(friendly)
        else:
            display_error = "{} did not respond".format(friendly)
    else:
        display_error = None

    return {
        "endpoint": endpoint,
        "method": method,
        "status": status,
        "passed": passed,
        "response_time_ms": elapsed_ms,
        "error": display_error,
        "body_preview": body_text or "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _get_blocked_hosts() -> list[str]:
    """Build the production hosts blocklist from config and environment."""
    blocked = [
        h.strip()
        for h in os.environ.get("BLOCKED_SMOKE_HOSTS", "").split(",")
        if h.strip()
    ]
    # Also block the configured production URL if set
    prod_url = config.PRODUCTION_BASE_URL
    if prod_url:
        # Extract the host portion (strip scheme)
        host = prod_url.replace("https://", "").replace("http://", "").rstrip("/")
        if host and host not in blocked:
            blocked.append(host)
    return blocked


def run_smoke_tests(base_url="http://localhost:8000", email=None, password=None):
    """
    Run the full smoke-test suite against *base_url*.

    SAFETY: Refuses to run against production hosts listed in
    ``BLOCKED_SMOKE_HOSTS`` env var or derived from ``config.PRODUCTION_BASE_URL``.

    Parameters
    ----------
    base_url : str
        Root URL of the running Flask app (no trailing slash).
    email : str or None
        Login email. Falls back to ``SMOKE_TEST_EMAIL`` env var,
        then ``config.SOAK_CHECK_EMAIL``.
    password : str or None
        Login password. Falls back to ``SMOKE_TEST_PASSWORD`` env var.

    Returns
    -------
    dict
        {
            "timestamp": "ISO-8601 string",
            "base_url": "...",
            "total": int,
            "passed": int,
            "failed": int,
            "results": [ ... ],
            "summary": "plain-English summary"
        }
    """
    # SAFETY: Block production targets
    for blocked in _get_blocked_hosts():
        if blocked in (base_url or "").lower():
            raise RuntimeError(
                f"SAFETY BLOCK: Smoke tests cannot run against production "
                f"host ({blocked}). Only the test VM is allowed."
            )

    email = email or os.environ.get("SMOKE_TEST_EMAIL", config.SOAK_CHECK_EMAIL)
    password = password or os.environ.get("SMOKE_TEST_PASSWORD", "")

    results = []
    auth_token = None
    login_failed = False

    # ------------------------------------------------------------------
    # 1. Health check (no auth)
    # ------------------------------------------------------------------
    results.append(
        _test_endpoint(base_url, "/health", method="GET")
    )

    # ------------------------------------------------------------------
    # 2. Login (no auth needed, but we harvest the token)
    # ------------------------------------------------------------------
    login_body = {"email": email, "password": password}

    def _check_login(status, body_text):
        """Return True only when the response contains a token."""
        try:
            data = json.loads(body_text)
            return bool(data.get("token") or data.get("access_token"))
        except Exception:
            return False

    login_result = _test_endpoint(
        base_url, "/api/auth/login", method="POST",
        body=login_body, check_fn=_check_login,
    )
    results.append(login_result)

    # Extract token if login succeeded
    if login_result["passed"]:
        try:
            resp_data = json.loads(login_result["body_preview"])
            auth_token = resp_data.get("token") or resp_data.get("access_token")
        except Exception:
            pass

    if auth_token is None:
        login_failed = True

    # ------------------------------------------------------------------
    # 3-9. Authenticated endpoints
    # ------------------------------------------------------------------
    auth_endpoints = [
        ("/api/dashboard/metrics", "GET"),
        ("/api/properties", "GET"),
        ("/accounting/api/tenants", "GET"),
        ("/api/maintenance/work-orders", "GET"),
        ("/leasing/api/applications", "GET"),
        ("/accounting/api/reports/chart-of-accounts", "GET"),
        ("/calendar/api/events", "GET"),
    ]

    for endpoint, method in auth_endpoints:
        friendly = _FRIENDLY_NAMES.get(endpoint, endpoint)
        if login_failed:
            # Mark as skipped -- don't penalize endpoints when login itself failed
            results.append({
                "endpoint": endpoint,
                "method": method,
                "status": None,
                "passed": True,   # not counted as a failure
                "skipped": True,
                "response_time_ms": 0,
                "error": "Skipped (login failed, so this could not be tested)",
                "body_preview": "",
            })
        else:
            headers = {"Authorization": "Bearer {}".format(auth_token)}
            results.append(
                _test_endpoint(base_url, endpoint, method=method,
                               headers=headers)
            )

    # ------------------------------------------------------------------
    # Build summary
    # ------------------------------------------------------------------
    total = len(results)
    skipped = sum(1 for r in results if r.get("skipped"))
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    # Human-friendly summary
    if login_failed:
        tested_count = total - skipped
        tested_passed = sum(1 for r in results if r["passed"] and not r.get("skipped"))
        failed_names = [
            _FRIENDLY_NAMES.get(r["endpoint"], r["endpoint"])
            for r in results if not r["passed"]
        ]
        if failed_names:
            summary = (
                "Login failed, so only {tested} of {total} checks could run. "
                "Issues found with: {names}. "
                "{skipped} checks were skipped because login was unavailable. "
                "The team will investigate."
            ).format(
                tested=tested_count,
                total=total,
                names=", ".join(failed_names),
                skipped=skipped,
            )
        else:
            summary = (
                "Login failed. {skipped} checks were skipped because login was "
                "unavailable. The app itself appears to be running. "
                "The team will investigate the login issue."
            ).format(skipped=skipped)
    elif failed == 0:
        summary = "All {} checks passed. The app is running normally.".format(total)
    else:
        failed_names = [
            _FRIENDLY_NAMES.get(r["endpoint"], r["endpoint"])
            for r in results if not r["passed"]
        ]
        summary = (
            "{passed} of {total} checks passed. "
            "Issues found with: {names}. "
            "The team will investigate."
        ).format(
            passed=passed,
            total=total,
            names=", ".join(failed_names),
        )

    # Strip body_preview from the public results (internal detail)
    clean_results = []
    for r in results:
        entry = {
            "endpoint": r["endpoint"],
            "method": r["method"],
            "status": r["status"],
            "passed": r["passed"],
            "response_time_ms": r["response_time_ms"],
            "error": r["error"],
        }
        if r.get("skipped"):
            entry["skipped"] = True
        clean_results.append(entry)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "total": total,
        "passed": passed,
        "failed": failed,
        "results": clean_results,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _print_results(report):
    """Pretty-print the smoke test report for terminal users."""
    print("")
    print("=" * 60)
    print("  SMOKE TEST REPORT")
    print("  {}".format(report["timestamp"]))
    print("  Target: {}".format(report["base_url"]))
    print("=" * 60)
    print("")

    for r in report["results"]:
        friendly = _FRIENDLY_NAMES.get(r["endpoint"], r["endpoint"])
        if r.get("skipped"):
            icon = "SKIP"
            line = "  [{}]  {} -- skipped (login unavailable)".format(icon, friendly)
        elif r["passed"]:
            icon = " OK "
            line = "  [{}]  {} -- working ({}ms)".format(icon, friendly, r["response_time_ms"])
        else:
            icon = "FAIL"
            reason = r["error"] or "HTTP {}".format(r["status"])
            line = "  [{}]  {} -- {}".format(icon, friendly, reason)
        print(line)

    print("")
    print("-" * 60)
    print("  {}".format(report["summary"]))
    print("-" * 60)
    print("  Passed: {} / {}".format(report["passed"], report["total"]))
    if report["failed"]:
        print("  Failed: {}".format(report["failed"]))
    print("")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="AI Ops Smoke Test Suite")
    parser.add_argument(
        "--base-url", default="http://localhost:8000",
        help="Base URL of the running app (default: http://localhost:8000)",
    )
    parser.add_argument("--email", default=None, help="Login email")
    parser.add_argument("--password", default=None, help="Login password")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    report = run_smoke_tests(
        base_url=args.base_url,
        email=args.email,
        password=args.password,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_results(report)

    # Exit code: 0 if all passed, 1 if any failed
    sys.exit(0 if report["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
