"""SS-10 Slice G — app-wiring assertions for the sandbox admin router.

Source-level assertions are intentional here: they prove the wiring shape
exists in ``gdx_dispatch/app.py`` without requiring the full app-factory graph to
import, which keeps this test stable across unrelated router churn.
A single runtime assertion additionally confirms that the router object
actually registers a route under ``/api/admin/sandbox``.
"""
from __future__ import annotations

from pathlib import Path

APP_PY = Path(__file__).resolve().parents[1] / "app.py"


def _app_source() -> str:
    return APP_PY.read_text(encoding="utf-8")


def test_app_source_imports_sandbox_admin() -> None:
    source = _app_source()
    assert "from gdx_dispatch.routers import sandbox_admin" in source, (
        "gdx_dispatch/app.py must defensively import sandbox_admin"
    )
    assert 'Failed to import router: sandbox_admin' in source, (
        "sandbox_admin import block must log on failure per app.py convention"
    )


def test_app_source_includes_sandbox_admin_router() -> None:
    source = _app_source()
    assert (
        'app.include_router(sandbox_admin.router if hasattr(sandbox_admin, "router") else sandbox_admin)'
        in source
    ), "gdx_dispatch/app.py must include the sandbox_admin router in create_app()"


def test_sandbox_admin_router_registers_prefix() -> None:
    from gdx_dispatch.routers import sandbox_admin

    router = sandbox_admin.router
    assert router.prefix == "/api/admin/sandbox"
    assert any(
        getattr(r, "path", "").startswith("/api/admin/sandbox") for r in router.routes
    ), "sandbox_admin router must expose at least one /api/admin/sandbox route"
