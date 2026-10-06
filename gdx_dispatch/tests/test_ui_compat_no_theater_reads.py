"""No ui_compat GET may answer a hardcoded empty payload (GDXA-314).

A GET whose whole body is ``return {"items": []}`` (or ``_empty_list()``, or a
dict of empty lists/zeros/None/False) is a "theater read": the page renders
"nothing here" for every tenant forever, which is indistinguishable from real
data being empty. ``GET /api/dispatch/optimize-route`` went one worse — the
Dispatch board toasted "Route order completed with no stops" off it. That one
and ``GET /api/maps`` were removed in GDXA-317.

``KNOWN_EMPTY_STUBS`` names the ones still on the branch. It is exact, not a
ceiling: whoever removes or fixes one of them removes its entry here, and an
entry with no matching handler fails the test so the list cannot rot.
"""
from __future__ import annotations

import ast
from pathlib import Path

from gdx_dispatch.routers import ui_compat

# Each sibling of GDXA-314 deletes its own entries as it lands.
KNOWN_EMPTY_STUBS: set[str] = set()

_EMPTY_HELPERS = {"_empty_list"}


def _is_empty_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in (None, 0, False, "")
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return all(v is not None and _is_empty_literal(v) for v in node.values)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id in _EMPTY_HELPERS and not node.args
    return False


def _get_path(fn: ast.FunctionDef) -> str | None:
    for dec in fn.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "get"
            and isinstance(dec.func.value, ast.Name)
            and dec.func.value.id == "router"
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
        ):
            return dec.args[0].value
    return None


def theater_reads(source: str) -> set[str]:
    """Paths of ``@router.get`` handlers whose only statement returns an empty literal."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        path = _get_path(node)
        if path is None:
            continue
        body = [
            s for s in node.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        ]
        if (
            len(body) == 1
            and isinstance(body[0], ast.Return)
            and body[0].value is not None
            and _is_empty_literal(body[0].value)
        ):
            found.add(path)
    return found


def test_detector_catches_the_removed_shapes():
    """Falsifier: the two bodies GDXA-317 deleted must be caught, and a real
    read must not be."""
    src = '''
@router.get("/api/dispatch/optimize-route", response_model=None)
def a(date=None):
    return {"stops": [], "total_distance_km": 0, "total_duration_sec": 0}

@router.get("/api/maps", response_model=None)
def b():
    """doc"""
    return {"tech_locations": [], "route_optimizations": []}

@router.get("/api/voice")
def c():
    return _empty_list()

@router.get("/api/real")
def d(db):
    rows = db.execute("x").all()
    return {"items": rows}

@router.post("/api/write")
def e():
    return {"items": []}
'''
    assert theater_reads(src) == {"/api/dispatch/optimize-route", "/api/maps", "/api/voice"}


def test_no_new_theater_reads_in_ui_compat():
    found = theater_reads(Path(ui_compat.__file__).read_text(encoding="utf-8"))
    new = found - KNOWN_EMPTY_STUBS
    assert not new, (
        f"ui_compat GET(s) return a hardcoded empty payload: {sorted(new)}. "
        "Read real data, or delete the route and make the view say the feature "
        "is not available."
    )


def test_known_empty_stub_list_is_not_stale():
    found = theater_reads(Path(ui_compat.__file__).read_text(encoding="utf-8"))
    stale = KNOWN_EMPTY_STUBS - found
    assert not stale, (
        f"KNOWN_EMPTY_STUBS names routes that are no longer empty stubs: {sorted(stale)}. "
        "Remove them from the list."
    )


def test_gdxa_317_routes_stay_gone():
    """Deleted, not reworded: ui_compat registers neither GET any more. The
    real `/api/maps/*` routes (maps.py, maps_provider) are untouched."""
    gets = {
        r.path for r in ui_compat.router.routes
        if "GET" in (getattr(r, "methods", None) or set())
    }
    assert "/api/dispatch/optimize-route" not in gets
    assert "/api/maps" not in gets


GDXA_315_REMOVED = {
    "/api/payroll/pay-periods",
    "/api/payroll/pay-stubs",
    "/api/quickbooks",
    "/api/voice",
    "/api/users/staff",
}


def test_gdxa_315_routes_stay_gone_from_the_app():
    """The five uncalled stubs are deleted, not reworded — and no other router
    picked the paths up. Checked against the whole app, so a stub re-added in
    any router is caught, not only one re-added in ui_compat. The real
    neighbours (/api/quickbooks/*, /api/timeclock/pay-periods, /api/users)
    stay registered. Read from the published route table, the same one
    openapi_routes.txt is gated against — `app.routes` at import time holds
    only /docs, so a check over it would pass on anything."""
    from gdx_dispatch.tools import openapi_snapshot as snap

    gets = {p for m, p in snap.operations(snap.build_spec()) if m == "GET"}
    assert not (GDXA_315_REMOVED & gets), sorted(GDXA_315_REMOVED & gets)
    assert {"/api/timeclock/pay-periods", "/api/quickbooks/recurring-transactions"} <= gets
