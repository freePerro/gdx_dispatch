"""GDXA-355: app.py router wiring fails closed.

app.py used to wrap every router import in a try/except that logged the
failure and bound an empty ``APIRouter()`` in its place, so a module that
would not import in the production image took its routes away while
``/health`` stayed green. Routers now load through ``_load_router``, which
re-raises unless the surface is on ``_OPTIONAL_ROUTERS``.

The scan below keeps the old shape out of app.py; the checker is run against
the shapes it must reject, so it is shown able to fail.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_PY = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def gdx_client():
    from fastapi.testclient import TestClient

    from gdx_dispatch.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


def _calls_name(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        for n in ast.walk(node)
    )


def _wires_a_router(stmts: list[ast.stmt]) -> bool:
    """True when the statements bind a router or hand one to include_router.

    A router module (``from gdx_dispatch.routers import jobs``) or any imported
    name with "router" in it counts; other guarded imports in app.py (the
    middleware stack, the /health denylist probe, startup bootstraps) do not.
    """
    for stmt in stmts:
        if _calls_name(stmt, "_load_router"):
            return True
        for n in ast.walk(stmt):
            if isinstance(n, ast.Import) and any(
                a.name.startswith("gdx_dispatch.routers") for a in n.names
            ):
                return True
            if isinstance(n, ast.ImportFrom) and (
                n.module == "gdx_dispatch.routers"
                or any("router" in (a.asname or a.name).lower() or "router" in a.name.lower() for a in n.names)
            ):
                return True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "include_router":
                return True
    return False


def _violations(source: str) -> list[str]:
    """Every fail-open router-wiring site in ``source``, as ``line: reason``."""
    tree = ast.parse(source)
    found: list[str] = []

    allowed_ctor_lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "_OPTIONAL_ROUTERS" for t in targets):
                allowed_ctor_lines.update(getattr(n, "lineno", -1) for n in ast.walk(node))

    for n in ast.walk(tree):
        # An empty router built anywhere but the optional allow-list is a
        # stand-in for one that did not load: an except-branch fallback, or a
        # getattr(..., APIRouter()) / `x if hasattr(...) else APIRouter()` default.
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "APIRouter"
            and n.lineno not in allowed_ctor_lines
        ):
            found.append(f"{n.lineno}: APIRouter() outside _OPTIONAL_ROUTERS")
        # A try that imports or includes a router and has a handler that does
        # not re-raise swallows the failure, whatever it binds instead.
        if isinstance(n, ast.Try) and _wires_a_router(n.body):
            for handler in n.handlers:
                if not any(isinstance(x, ast.Raise) for x in ast.walk(handler)):
                    found.append(f"{n.lineno}: try/except around router wiring does not re-raise")
    return found


def test_app_py_has_no_fail_open_router_wiring():
    assert _violations(APP_PY.read_text()) == []


@pytest.mark.parametrize(
    "snippet",
    [
        # the 134-handler shape this replaced
        "try:\n"
        "    from gdx_dispatch.routers import jobs\n"
        "except Exception:\n"
        "    log.exception('x')\n"
        "    jobs = APIRouter(prefix='/jobs')\n",
        # the double-router shape: one failure empties a sibling
        "try:\n"
        "    from gdx_dispatch.core.payments import public_router, router\n"
        "except Exception:\n"
        "    public_router = router = None\n",
        # the inline create_app() shape: import and include, log on failure
        "def create_app():\n"
        "    try:\n"
        "        from gdx_dispatch.routers import voice\n"
        "        app.include_router(voice.router)\n"
        "    except Exception:\n"
        "        log.exception('x')\n",
        # the helper itself, swallowed
        "try:\n"
        "    jobs = _load_router('gdx_dispatch.routers', 'jobs')\n"
        "except Exception:\n"
        "    jobs = None\n",
        # a plain module import of a router, swallowed
        "try:\n"
        "    import gdx_dispatch.routers.jobs as jobs\n"
        "except ImportError:\n"
        "    jobs = None\n",
        # a default fallback with no try at all
        "app.include_router(getattr(m, 'staff_router', APIRouter()))\n",
    ],
)
def test_checker_rejects_the_fail_open_shapes(snippet):
    assert _violations(snippet), "the guard cannot fail for this shape"


def test_checker_accepts_a_handler_that_re_raises():
    snippet = (
        "try:\n"
        "    from gdx_dispatch.routers import jobs\n"
        "except Exception:\n"
        "    log.critical('x')\n"
        "    raise\n"
    )
    assert _violations(snippet) == []


def test_load_router_raises_for_a_required_router(monkeypatch):
    from gdx_dispatch import app as app_mod

    fired: list[str] = []
    monkeypatch.setattr(app_mod, "ROUTER_FALLBACKS", fired)
    with pytest.raises(ModuleNotFoundError):
        app_mod._load_router("gdx_dispatch.routers", "no_such_router_gdxa_355")
    assert fired == []


def test_load_router_falls_back_for_an_optional_router(monkeypatch):
    from fastapi import APIRouter

    from gdx_dispatch import app as app_mod

    fallback = APIRouter(tags=["gdxa-355"])
    fired: list[str] = []
    monkeypatch.setattr(app_mod, "ROUTER_FALLBACKS", fired)
    monkeypatch.setitem(
        app_mod._OPTIONAL_ROUTERS, "gdx_dispatch.routers.no_such_router_gdxa_355", fallback
    )
    got = app_mod._load_router("gdx_dispatch.routers", "no_such_router_gdxa_355")
    assert got is fallback
    assert fired == ["gdx_dispatch.routers.no_such_router_gdxa_355"]


def test_load_router_mirrors_from_import():
    """``_load_router(pkg, name)`` binds what ``from pkg import name`` binds."""
    from gdx_dispatch import app as app_mod
    from gdx_dispatch.core.payments import public_router
    from gdx_dispatch.routers import voice

    assert app_mod._load_router("gdx_dispatch.routers", "voice") is voice
    assert app_mod._load_router("gdx_dispatch.core.payments", "public_router") is public_router


def test_health_names_a_fired_fallback(gdx_client, monkeypatch):
    from gdx_dispatch import app as app_mod

    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(app_mod, "ROUTER_FALLBACKS", ["gdx_dispatch.routers.pdf"])
    rv = gdx_client.get("/health")
    assert rv.status_code == 200, rv.text
    assert rv.json()["router_fallbacks"] == "gdx_dispatch.routers.pdf"


def test_health_omits_router_fallbacks_when_none_fired(gdx_client, monkeypatch):
    from gdx_dispatch import app as app_mod

    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(app_mod, "ROUTER_FALLBACKS", [])
    rv = gdx_client.get("/health")
    assert rv.status_code == 200, rv.text
    assert "router_fallbacks" not in rv.json()
