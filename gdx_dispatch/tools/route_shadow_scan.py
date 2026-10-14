#!/usr/bin/env python
"""Route-shadow scanner.

Two FastAPI handlers registered against the same `(method, path)` pair
silently collide — whichever was `include_router`'d first wins; the
other is dead code that flips behavior the moment include order changes.
The Settings → Modules empty-panel bug (S100) was one shadowed pair
(`branding_public.py` ↔ `settings.py`) that returned a thin shape and
broke the admin tab.

Usage:
    python gdx_dispatch/tools/route_shadow_scan.py            # human-readable report
    python gdx_dispatch/tools/route_shadow_scan.py --json     # machine output
    python gdx_dispatch/tools/route_shadow_scan.py --check    # exit 1 on net-new vs baseline

Baseline lives in `gdx_dispatch/tools/route_shadow_baseline.txt` — one
`METHOD path` line per known-shadowed pair. The cleanup sprint
(`ai-queue/plans/sprint_three_plane_cleanup.md`, S2/S3) drives the
baseline to zero; this script gates net-new shadows in CI.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = REPO_ROOT / "gdx_dispatch" / "tools" / "route_shadow_baseline.txt"


def collect_shadows() -> dict[tuple[str, str], list[str]]:
    """Walk the live FastAPI app and group routes by (method, path)."""
    import os

    sys.path.insert(0, str(REPO_ROOT))
    # Mirror gdx_dispatch/tests/conftest.py — give auth.py a deterministic HS256 secret
    # so we can import the app without a real keypair. Standalone CLI runs hit
    # this; pytest already sets it before our test module imports.
    os.environ.setdefault(
        "JWT_SECRET",
        "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety",
    )
    from gdx_dispatch.app import app

    # 2026-08-24 FIX — this scanner was blind for its entire life.
    #
    # It iterated `app.routes` FLAT. FastAPI >=0.137 no longer flattens
    # `include_router()` into `app.routes`: each include leaves a lazy
    # `_IncludedRouter` wrapper with NO `.path` and NO `.endpoint`, so the loop
    # `continue`d past every one of them. The scan saw ~10 of 1442 routes and
    # reported ZERO shadows while 43 were live — and because
    # `route_shadow_baseline.txt` was never generated, `load_baseline()`
    # returned an empty set and `test_no_net_new_route_shadows` compared
    # nothing to nothing and passed. A green gate that could not fail for the
    # defect it exists to catch.
    #
    # Recurse the wrappers, re-applying each include prefix — same traversal as
    # `gdx_dispatch/tests/conftest.py::iter_app_routes` and
    # `gdx_dispatch/tools/frontend_contract_scan.py::routes_from_app`.
    try:
        from fastapi.routing import _IncludedRouter
    except ImportError:  # older FastAPI — routes already flat
        _IncludedRouter = ()

    def _walk(routes, prefix: str = ""):
        for route in routes or ():
            if _IncludedRouter and isinstance(route, _IncludedRouter):
                sub = prefix + getattr(route.include_context, "prefix", "")
                yield from _walk(route.original_router.routes, sub)
                continue
            path = getattr(route, "path", None)
            sub_routes = getattr(route, "routes", None)
            if sub_routes and not getattr(route, "methods", None):  # Mount
                yield from _walk(sub_routes, prefix + (path or ""))
                continue
            if path is not None:
                yield prefix + path, route

    shadows: dict[tuple[str, str], list[str]] = defaultdict(list)
    for full_path, route in _walk(app.routes):
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        if not methods or endpoint is None:
            continue
        # Skip starlette mounts and websockets — only HTTP method routes.
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            module = getattr(endpoint, "__module__", "<unknown>")
            qualname = getattr(endpoint, "__qualname__", endpoint.__name__)
            shadows[(method, full_path)].append(f"{module}:{qualname}")

    return {key: handlers for key, handlers in shadows.items() if len(handlers) > 1}


def load_baseline() -> set[tuple[str, str]]:
    if not BASELINE_FILE.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for line in BASELINE_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            out.add((parts[0].upper(), parts[1]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 on net-new shadows vs baseline.")
    parser.add_argument("--write-baseline", action="store_true",
                        help="Write current shadows to baseline file.")
    args = parser.parse_args()

    shadows = collect_shadows()
    keys = sorted(shadows.keys())

    if args.write_baseline:
        BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with BASELINE_FILE.open("w") as fh:
            fh.write("# Generated by gdx_dispatch/tools/route_shadow_scan.py --write-baseline\n")
            fh.write("# One METHOD path per line. Goal: drive to zero via\n")
            fh.write("# ai-queue/plans/sprint_three_plane_cleanup.md S2/S3.\n")
            for method, path in keys:
                fh.write(f"{method} {path}\n")
        print(f"Wrote {len(keys)} shadows to {BASELINE_FILE}")
        return 0

    if args.json:
        print(json.dumps({f"{m} {p}": h for (m, p), h in shadows.items()}, indent=2))
    else:
        print(f"route_shadow_scan: {len(shadows)} shadowed (method, path) pairs")
        for method, path in keys:
            print(f"  {method} {path}")
            for h in shadows[(method, path)]:
                print(f"    {h}")

    if args.check:
        baseline = load_baseline()
        net_new = set(keys) - baseline
        if net_new:
            print(f"\nFAIL: {len(net_new)} net-new route shadow(s) vs baseline:",
                  file=sys.stderr)
            for method, path in sorted(net_new):
                print(f"  {method} {path}", file=sys.stderr)
            print(f"\nIf intentional, run: python {Path(__file__).name} --write-baseline",
                  file=sys.stderr)
            return 1
        if len(keys) < len(baseline):
            print(f"\nNote: shadow count dropped from {len(baseline)} → {len(keys)}; "
                  "rebaseline with --write-baseline to lock the win.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
