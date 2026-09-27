"""Every router this tree defines is mounted on the app, or allow-listed with a reason.

The class this guards: **a router that is defined, decorated, imported and
tested, but never included on the app — while a twin serves its path.**
`modules/quickbooks/webhook_router.py` is the instance that prompted it (GDXA-36,
`FOUND_NOT_FILED.md:819`): one `POST /api/qb/webhook` handler that no commit ever
wired, whose regression net (11 import sites across 3 test files) calls the
function directly and so stayed green for the whole time the route did not exist.

Why a *test* and not a probe: on this app an unmounted POST path answers **405**,
not 404, because the SPA catch-all `GET /{full_path:path}` absorbs it. A status
code cannot answer "is this route registered?" here. And a flat read of
`app.routes` answers it wrongly — FastAPI >=0.137 defers each `include_router()`
behind a lazy `_IncludedRouter` wrapper, so the flat list holds 193 entries of
which only **7** carry `.path` (measured 2026-09-27) and every router looks
unmounted. Both false signals were hit live during triage.

How the check actually works, and why identity:

1. AST-collect the module-level assigned names of every file that mentions
   `APIRouter` (module level only — a name bound by `from x import router` is an
   `ImportFrom`, not an `Assign`, so a foreign router is never attributed here).
2. Import the file and keep the names that really hold an `APIRouter`. This
   catches what a pure source scan cannot: an alias (`router = distributor_router`)
   and a router assembled at runtime.
3. Ask whether any of that router's own route objects is reachable in
   `iter_app_routes(create_app())` **by `id()`**. The lazy wrapper yields
   `original_router.routes` — the sub-router's *own* objects — so identity is
   exact. It cannot be fooled by a same-named path served by a different file,
   which is precisely the twin case this guard exists for. Verified 2026-09-27:
   `routers/jobs.py` 26/26 own routes hit, `modules/ledger/router.py` 14/14,
   `modules/distributor/order_portal.py` 8/8 + 3/3.

A router carrying zero routes is skipped — there is nothing to mount, and
`app.py` builds exactly that as its import-failure fallback (`app.py:236` and
friends). `routers/marketing.py` is a real, mounted, empty router.

**This set is not new, and that is the point.** A static-analysis probe recorded
the same class on 2026-09-13, in the maintainer-local ledger: "27 decorated route
handlers in 7 router objects are never mounted; 10 share a (method, path) with a
live route", citing `core/gdpr.py` `DELETE /api/customers/{customer_id}` as its
example. Two of those 7 — `modules/equipment/router.py` and
`modules/fleet/router.py` — were deleted on 2026-09-14 by `625467f9` (#735,
"Customer Equipment and Fleet, never used, are gone"), which leaves exactly the 5
allow-listed below. That a decorator-scanning probe and this identity-based route
walk independently land on the same set is the best evidence available that the
discovery is complete; it also means the probe's finding sat open for two weeks
with nothing to keep it from growing. That is what a guard buys over a probe.

**What makes this guard non-blind.** A test that asserts "nothing is unmounted"
passes identically when the detector is broken. Three things here can only be
green for the right reason: `test_allow_list_entries_are_all_still_unmounted`
re-derives every allow-listed entry from the live app, so if the AST step, the
import step or the identity step goes blind, all five entries fail at once and
name themselves; `test_discovery_is_not_vacuous` floors the router count and the
leaf-route count, so an empty scan cannot pass; and
`test_import_failures_are_loud` refuses to silently drop a router file that
stopped importing — the failure mode that once cost 17 mobile endpoints.

Falsifier for the whole approach, named so the next reader can go look. Three
things this cannot see:

* A router mounted by something other than `include_router` on the app object —
  an `app.mount()` of a sub-application. The MCP server at `/mcp` is a real
  `app.mount()` and is deliberately out of scope; `core/mcp_mount.py` has its own
  tests (`test_mcp_mount_under_loop.py`, `test_mcp_streamable_http_mount.py`).
* A router built inside a function and never bound to a module-level name.
* A router belonging to a *different* FastAPI application in this same package.
  `plugin_host/` is exactly that (`plugin_host/app.py:95` builds a second
  `FastAPI`), and `plugin_api/`'s manifest ABI hands every plugin a `router`
  (`plugin_api/manifest.py:81`). Both are in `SKIP_DIRS` — if they were not, this
  guard would be green only by luck, because neither binds a module-level
  `APIRouter` today. Checked 2026-09-27.

The complement to this guard is `test_route_shadow_baseline.py`: this file catches
a router that is *defined but never reached*, that one catches a route that is
*mounted but shadowed by an earlier registration*. Between them the two halves of
"the code you are reading is not the code that serves this path" are covered.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

# Imported for the flattener, and for the JWT_SECRET/GDX_TENANT_ID that its own
# module-level setdefault calls install (conftest.py:15,:23) — auth refuses to
# import without a signing key, so this import has to come before any app import.
from gdx_dispatch.tests.conftest import iter_app_routes

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Not application wiring: the frontend, the suite itself, Alembic revisions,
# generated/vendored trees. Everything else in the package is in scope — the
# defect shape is not confined to `modules/`, and restricting the scan to
# `modules/**/*router*.py` would have missed 2 of the 5 entries below.
#
# `plugin_host` and `plugin_api` are excluded for a different reason, and it is
# not cosmetic: they belong to a SECOND FastAPI application (plugin_host/app.py:95)
# whose routers are never meant to reach `create_app()`. Including them would make
# every plugin router a false positive the day one binds a module-level `APIRouter`.
SKIP_DIRS = frozenset({
    "frontend", "tests", "migrations", "node_modules", "__pycache__",
    "docker", "docs", "templates", "static",
    "plugin_host", "plugin_api",
})


# ── The allow-list ───────────────────────────────────────────────────────────
#
# Key: "<path relative to gdx_dispatch/>::<module-level name>".
# Value: why it is not mounted. A reason is mandatory and is asserted to be
# substantive — "" or "TODO" is a red test, because an allow-list whose entries
# carry no reason is just a suppression.
#
# Adding an entry here is a *record*, not a fix. Only the QuickBooks one has a
# ruling; the other four are the 2026-09-13 probe's open finding, re-derived by
# execution here on 2026-09-27, and the disposition of each is Doug's. Two of them
# must stay unmounted and say why.
#
# Ledger references below give a date and a quote, not a line number: the ledger
# (`FOUND_NOT_FILED.md`) is untracked and maintainer-local by design, so it does
# not exist in this worktree and its line numbers shift on every append.

UNMOUNTED_BY_DESIGN: dict[str, str] = {
    "modules/quickbooks/webhook_router.py::router": (
        "unmounted, retiring — the one entry here with a ruling: Doug, 2026-09-13, "
        "'its dispatcher modules/quickbooks/webhook_router.py is never mounted; "
        "handle them under QuickBooks retirement'. Retirement intent corroborated "
        "by UNFINISHED_WORK.md ('a QuickBooks we can no longer reach', 'retired by "
        "the QuickBooks phase-out') — note that file does not mention this router. "
        "It declares POST /api/qb/webhook; the live twin is POST /api/qb/webhooks "
        "on modules/quickbooks/router.py:1070. DO NOT MOUNT AS-IS: "
        "webhook_router.py:131 gates the whole signature check behind "
        "`if verifier_token:`, so with neither QB_WEBHOOK_VERIFIER_TOKEN nor "
        "QB_WEBHOOK_SECRET set it processes unsigned bodies — mounting it ships an "
        "unauthenticated write endpoint. GDXA-36 / GDXA-92."
    ),
    "core/gdpr.py::gdpr_router": (
        "MUST STAY UNMOUNTED. It declares DELETE /api/customers/{customer_id} — "
        "the same (method, path) as the live SOFT delete at "
        "routers/customers.py:706 (`customer.deleted_at = now`) — but "
        "hard-anonymises PII in place (name -> sentinel, email/phone/hashes "
        "nulled) and carries no require_role dependency. Mounting it would put a "
        "second, ungated, irreversible handler on a path the app already serves, "
        "against ARCHITECTURAL_INVARIANTS.md invariant #2 (soft-delete on tables "
        "carrying deleted_at). The gated GDPR surface already exists and is "
        "mounted: POST /api/gdpr/delete-customer/{customer_id} and "
        "GET /api/gdpr/export-customer/{customer_id} on routers/gdpr.py, both "
        "behind require_role('admin','owner'). The module's FUNCTIONS are live "
        "(core/gdpr.delete_customer_data / export_customer_data); only this "
        "router is dead. This is the 2026-09-13 ledger entry's own worked example "
        "('10 share a (method, path) with a live route', e.g. core/gdpr.py:60); "
        "re-derived by execution 2026-09-27. Deletion candidate, not adjudicated."
    ),
    "modules/purchase_orders/router.py::router": (
        "unmounted duplicate, already adjudicated elsewhere: app.py:1473 mounts "
        "routers/purchase_orders.py, and test_dead_duplicates_retired.py's "
        "RESOLVED_PAIRS pins GET/POST /api/purchase-orders and "
        "POST /api/purchase-orders/{po_id}/receive to "
        "gdx_dispatch.routers.purchase_orders with exactly ONE registration each "
        "(#568). This twin declares those same three paths, so mounting it would "
        "redden that test. Four of its five paths are live already; only "
        "POST /api/purchase-orders/{po_id}/send has no counterpart. One of the "
        "2026-09-13 ledger entry's 7; re-derived by execution 2026-09-27."
    ),
    "modules/maintenance/router.py::router": (
        "unmounted duplicate: app.py:1477 mounts routers/maintenance.py, whose "
        "GET/POST /api/maintenance/plans (routers/maintenance.py:218,:236) are "
        "the same paths this twin declares. Its other four routes are a renamed "
        "surface, not a missing one: /api/customers/{customer_id}/plan{,/enroll, "
        "/cancel} and /api/maintenance/upcoming are served live as "
        "GET/POST /api/maintenance/enrollments, "
        "POST /api/maintenance/enrollments/{id}/advance and "
        "GET /api/maintenance/due-this-month. So the capability is reachable and "
        "this file is dead weight, not a gap. One of the 2026-09-13 ledger entry's "
        "7; re-derived by execution 2026-09-27. Deletion candidate, not adjudicated."
    ),
    "core/circuit_breaker.py::router": (
        "unmounted admin surface, no twin and no caller: GET /admin/circuit-breakers "
        "and POST /admin/circuit-breakers/{name}/reset. The string "
        "'circuit-breakers' appears nowhere else in gdx_dispatch/ and nowhere in "
        "frontend/src, so a tripped breaker can be neither read nor reset over "
        "HTTP. The breaker objects themselves ARE live — app.py:929 imports them "
        "for their startup side effect. Note the prefix is /admin, not /api/admin, "
        "which is off-pattern for every other admin router here. One of the "
        "2026-09-13 ledger entry's 7; re-derived by execution 2026-09-27. "
        "Not adjudicated."
    ),
}


# ── Discovery ────────────────────────────────────────────────────────────────

def _module_level_assigned_names(tree: ast.Module) -> set[str]:
    """Names assigned at module level, including inside module-level try/if.

    Bodies of function and class definitions are not descended into: a router
    built inside a function is out of this guard's reach (named in the docstring
    falsifier). An `ImportFrom` is not an `Assign`, so a router imported from
    another file is never attributed to this one.
    """
    names: set[str] = set()

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(node, attr, None)
                if isinstance(inner, list):
                    walk(inner)
            for handler in getattr(node, "handlers", []) or []:
                walk(handler.body)

    walk(tree.body)
    return names


def _candidate_files() -> list[pathlib.Path]:
    out = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            if "APIRouter" in path.read_text():
                out.append(path)
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable source
            continue
    return out


@pytest.fixture(scope="module")
def discovered() -> tuple[dict[str, object], list[str]]:
    """({"relpath::name": router}, [import failures]) for every router defined here.

    Aliases are collapsed by object identity, taking the alphabetically first
    name, so `order_portal.py`'s `router`/`distributor_router` pair counts once.
    """
    from fastapi import APIRouter

    routers: dict[str, object] = {}
    failures: list[str] = []

    for path in _candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            assigned = _module_level_assigned_names(ast.parse(path.read_text()))
        except SyntaxError as exc:  # pragma: no cover - would break far more than this test
            failures.append(f"{rel}: unparseable ({exc})")
            continue
        if not assigned:
            continue
        dotted = "gdx_dispatch." + rel[: -len(".py")].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        try:
            mod = importlib.import_module(dotted)
        except Exception as exc:  # noqa: BLE001 - the whole point is to name it
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            continue
        seen: set[int] = set()
        for name in sorted(assigned):
            obj = getattr(mod, name, None)
            if isinstance(obj, APIRouter) and id(obj) not in seen:
                seen.add(id(obj))
                routers[f"{rel}::{name}"] = obj
    return routers, failures


@pytest.fixture(scope="module")
def live_route_ids() -> set[int]:
    """`id()` of every leaf route object the app actually serves."""
    from gdx_dispatch.app import create_app

    return {id(route) for _path, route in iter_app_routes(create_app())}


def _unmounted(discovered, live_route_ids) -> dict[str, list[str]]:
    """{key: [paths it would have served]} for routers contributing no live route."""
    routers, _failures = discovered
    out: dict[str, list[str]] = {}
    for key, router in sorted(routers.items()):
        own = list(router.routes)
        if not own:
            continue  # nothing to mount; app.py's import-failure fallbacks land here
        if any(id(route) in live_route_ids for route in own):
            continue
        out[key] = sorted(getattr(r, "path", "<no path>") for r in own)
    return out


# ── The guard ────────────────────────────────────────────────────────────────

def test_every_router_is_mounted_or_allow_listed(discovered, live_route_ids) -> None:
    unmounted = _unmounted(discovered, live_route_ids)
    undeclared = {k: v for k, v in unmounted.items() if k not in UNMOUNTED_BY_DESIGN}
    assert not undeclared, (
        "these routers are defined and carry routes, but the app serves none of them.\n"
        "Mount it in app.py, delete it, or add it to UNMOUNTED_BY_DESIGN with a reason:\n"
        + "\n".join(f"  {k}\n      would serve: {', '.join(v)}" for k, v in undeclared.items())
    )


def test_allow_list_entries_are_all_still_unmounted(discovered, live_route_ids) -> None:
    """Re-derive every allow-listed entry from the live app.

    This is the half that makes the guard above non-blind. If the AST step, the
    import step or the `id()` identity step stops working, every entry fails here
    and names itself — instead of the guard going quietly green. It also retires
    an entry the moment someone mounts or deletes it, so the list cannot rot into
    a list of files that no longer exist.
    """
    routers, _failures = discovered
    unmounted = _unmounted(discovered, live_route_ids)
    stale = []
    for key in sorted(UNMOUNTED_BY_DESIGN):
        if key not in routers:
            stale.append(f"  {key}: no longer defines that router (deleted or renamed)")
        elif key not in unmounted:
            stale.append(f"  {key}: is mounted now — drop the allow-list entry")
    assert not stale, "UNMOUNTED_BY_DESIGN has stale entries:\n" + "\n".join(stale)


def test_discovery_is_not_vacuous(discovered, live_route_ids) -> None:
    """An empty scan would make every assertion here pass. Floor both ends.

    Measured 2026-09-27 in docker-app: 189 candidate files, 225 module-level
    routers, 1234 leaf route objects on `create_app()`. The floors sit well below
    both so ordinary growth or removal does not trip them, but a discovery step
    that returns nothing — or a flattener that regressed to the ~9-entry flat
    read — is red.
    """
    routers, _failures = discovered
    assert len(routers) >= 150, f"router discovery found only {len(routers)} — it is broken"
    assert len(live_route_ids) >= 900, (
        f"the app exposes only {len(live_route_ids)} leaf routes — "
        "iter_app_routes is probably no longer recursing the include wrappers"
    )


def test_import_failures_are_loud(discovered) -> None:
    """A router file that stopped importing must not be silently skipped.

    `app.py` wires most routers inside `try/except` that logs and continues, so a
    broken import degrades to a missing surface with a green suite — that is how
    17 mobile endpoints once vanished for want of `python-multipart`.
    """
    _routers, failures = discovered
    assert not failures, "router files that could not be imported:\n" + "\n".join(f"  {f}" for f in failures)


@pytest.mark.parametrize("key", sorted(UNMOUNTED_BY_DESIGN))
def test_allow_list_reason_is_substantive(key: str) -> None:
    """"With a reason" has to mean something, or the list is just a suppression."""
    reason = UNMOUNTED_BY_DESIGN[key].strip()
    assert len(reason) >= 40, f"{key}: reason is too thin to be a record: {reason!r}"
    assert not reason.lower().startswith(("todo", "tbd", "fixme", "n/a")), f"{key}: {reason!r}"
