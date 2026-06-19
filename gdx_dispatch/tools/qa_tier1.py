#!/usr/bin/env python3
"""QA Tier 1 — API sweep with automatic systemic failure detection.

Hits all endpoints, groups failures by status code, decides whether to
continue to Tier 2 or stop for /debug.

Usage:
    python gdx_dispatch/tools/qa_tier1.py --token TOKEN --tenant SLUG [--base-url URL]

Exit codes:
    0 = all passed (Tier 2: critical path only)
    1 = independent failures (Tier 2: critical path + failure pages)
    2 = systemic failure (STOP — run /debug)
    3 = app unreachable (STOP — check containers)
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

BASELINE = Path(__file__).parent / "qa_baseline.json"

ENDPOINTS = [
    "/api/customers", "/api/estimates", "/api/invoices", "/api/jobs",
    "/api/leads", "/api/payments", "/api/technicians", "/api/users",
    "/api/timeclock/entries", "/api/fleet/vehicles", "/api/equipment",
    "/api/inventory/parts", "/api/proposals", "/api/campaigns",
    "/api/documents", "/api/tags", "/api/vendors", "/api/purchase-orders",
    "/api/scheduling", "/api/sticky-notes", "/api/communications/threads",
    "/api/reviews", "/api/photos/recent", "/api/commissions/summary",
    "/api/payroll/pay-periods", "/api/settings/branding",
    "/api/maintenance/plans", "/api/service-agreements",
    "/api/change-orders", "/api/collections",
]


def sweep(base_url: str, token: str, tenant: str) -> list[dict]:
    results = []
    headers = {
        "Authorization": f"Bearer {token}",
        "x-tenant-id": tenant,
        "User-Agent": "GDX-QA/1.0",
    }
    for ep in ENDPOINTS:
        url = f"{base_url}{ep}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                results.append({"endpoint": ep, "status": resp.status})
        except urllib.error.HTTPError as e:
            results.append({"endpoint": ep, "status": e.code})
        except Exception as e:
            results.append({"endpoint": ep, "status": 0, "error": str(e)})
    return results


def analyze(results: list[dict]) -> int:
    failures = [r for r in results if r["status"] >= 400 or r["status"] == 0]
    passed = [r for r in results if 200 <= r["status"] < 400]

    print(f"API Sweep: {len(passed)} passed, {len(failures)} failed out of {len(results)}")

    if not failures:
        print("✓ All endpoints healthy — Tier 2: critical path only")
        return 0

    # Check unreachable
    unreachable = [r for r in results if r["status"] == 0]
    if len(unreachable) > len(results) // 2:
        print("❌ App unreachable — most endpoints returned connection errors")
        print("   Check: docker ps, nginx, container health")
        return 3

    # Group by status code
    status_counts = Counter(r["status"] for r in failures)
    print(f"\nFailures by status: {dict(status_counts)}")
    for r in failures:
        print(f"  {r['status']} {r['endpoint']}")

    # Systemic check: does one status code dominate?
    dominant_code, dominant_count = status_counts.most_common(1)[0]
    if dominant_count > len(failures) // 2 and len(failures) > 2:
        print(f"\n❌ SYSTEMIC: {dominant_count}/{len(failures)} failures are {dominant_code}")
        print("   STOP — run /debug to find root cause, then re-run /qa")
        return 2

    # Independent failures
    print(f"\n⚠ {len(failures)} independent failures — Tier 2: critical path + these pages")
    return 1


def update_baseline(results: list[dict]):
    if not BASELINE.exists():
        return
    from datetime import date
    baseline = json.loads(BASELINE.read_text())
    today = str(date.today())
    for r in results:
        ep = r["endpoint"]
        if ep in baseline.get("api_endpoints", {}):
            baseline["api_endpoints"][ep]["status"] = r["status"]
            baseline["api_endpoints"][ep]["last_checked"] = today
    baseline["updated"] = today
    BASELINE.write_text(json.dumps(baseline, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description="QA Tier 1 API sweep")
    p.add_argument("--token", required=True, help="Bearer token")
    p.add_argument("--tenant", default="gdx", help="Tenant slug")
    p.add_argument("--base-url", default="https://gdx.example.com")
    args = p.parse_args()

    results = sweep(args.base_url, args.token, args.tenant)
    exit_code = analyze(results)
    update_baseline(results)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
