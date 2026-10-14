"""A router's module docstring route list matches the routes it registers.

GDXA-1018: ``routers/dispatch_scheduling.py`` advertised
``GET /api/dispatch/check-capacity — overbooking prevention`` from the initial
public release onward. No such route was ever added, and the docstring never
listed ``/api/dispatch/scheduled-unassigned``, which does exist. A wiki page
then copied the claim. The sweep that followed found ``outlook_oauth.py``
describing ``GET /start`` as a redirect for what has always been a POST that
answers JSON, and six more routers whose lists had fallen behind: SMS,
payment, credential and markup-rule routes added and never listed, or
folded into a "CRUD" line.

A route list here is any module-docstring line that starts with an HTTP
method followed by a path, bare, bulleted or in double backticks
(``GET /api/...``, ``PATCH/api/...``, ``- GET /api/...``,
````* ``GET /api/...`` ````). Every module under ``routers/``, subpackages
included, is scanned. A module that writes a list must list every route its
routers register, and nothing else. Prose that mentions a route mid-sentence
(dated history, another router's endpoint, a single-backticked path that
wraps onto a new line) does not count as a list and is not checked. A module
with no list is not checked either: having no list is fine, a wrong list is not.

The registered set comes from the imported ``APIRouter`` objects, not from
reading the decorators, so ``add_api_route`` and second routers count too.
Paths are the router-level paths (``prefix`` included); a module whose
routes get a further prefix at ``include_router`` time would show here as
a mismatch, which is the cue to write the list with the full path.
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.routing import APIRoute

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "routers"
_LIST_LINE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:``)?(GET|POST|PUT|PATCH|DELETE)[ \t]*(/[^\s?`]+)", re.M
)
_PARAM = re.compile(r"\{[^}]*\}")


def _key(method: str, path: str) -> tuple[str, str]:
    # Parameter names are prose in a docstring ({id} vs {invoice_id}).
    return method, _PARAM.sub("{}", path.rstrip("/") or "/")


def _documented(path: Path) -> set[tuple[str, str]]:
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    return {_key(m, p) for m, p in _LIST_LINE.findall(doc)}


def _registered(module_name: str) -> set[tuple[str, str]]:
    module = importlib.import_module(module_name)
    out: set[tuple[str, str]] = set()
    for obj in vars(module).values():
        if not isinstance(obj, APIRouter):
            continue
        for route in obj.routes:
            if isinstance(route, APIRoute):
                out |= {_key(m, route.path) for m in route.methods if m != "HEAD"}
    return out


def _module_name(path: Path) -> str:
    parts = path.relative_to(ROUTERS_DIR.parents[1]).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _listing_modules() -> dict[str, set[tuple[str, str]]]:
    return {
        _module_name(p): documented
        for p in sorted(ROUTERS_DIR.rglob("*.py"))
        if (documented := _documented(p))
    }


def test_the_scan_sees_route_lists():
    """If the regex or the walk stopped matching, every check below would pass
    vacuously. One module per list shape: bare, bulleted, double-backticked,
    and one in a subpackage."""
    modules = _listing_modules()
    assert len(modules) >= 25, sorted(modules)
    assert ("GET", "/api/dispatch/late-open") in modules["gdx_dispatch.routers.dispatch_scheduling"]
    assert ("POST", "/api/games/event") in modules["gdx_dispatch.routers.games"]
    assert ("GET", "/api/meta/versions") in modules["gdx_dispatch.routers.api_metadata"]
    assert ("GET", "/auth/sso/google") in modules["gdx_dispatch.routers.auth.sso"]


def test_every_router_docstring_route_list_matches_its_registered_routes():
    problems = []
    for name, documented in _listing_modules().items():
        registered = _registered(name)
        for method, path in sorted(documented - registered):
            problems.append(f"{name} lists {method} {path}, which it does not register")
        for method, path in sorted(registered - documented):
            problems.append(f"{name} registers {method} {path}, which its route list omits")
    assert not problems, "\n".join(problems)


def test_check_capacity_is_not_advertised():
    """The instance that named the class (GDXA-1018)."""
    documented = _documented(ROUTERS_DIR / "dispatch_scheduling.py")
    assert ("GET", "/api/dispatch/check-capacity") not in documented
    assert ("GET", "/api/dispatch/scheduled-unassigned") in documented
