"""Regression: create_app() must wire oauth2 router's get_db sentinel
to the real control-plane session factory.

The lab smoke at sprint-mcp-streamable-http S6 caught a 500 on
POST /oauth/register because the oauth2 router declares a get_db
sentinel that raises if not overridden, and create_app() never set the
override. This test asserts the override exists after create_app()
runs, so the bug can't re-appear silently.
"""
from __future__ import annotations

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import oauth2 as oauth2_mod


def test_create_app_overrides_oauth2_get_db():
    """Asserts the dependency_overrides hook is set, mapping the
    sentinel to get_db. Imported lazily so the side-effect
    construction (database engines, lifespan) doesn't fire at module
    collection time.
    """
    from gdx_dispatch.app import create_app

    app = create_app()

    assert oauth2_mod.get_db in app.dependency_overrides, (
        "create_app() must override oauth2.get_db; the router declares "
        "a sentinel that raises RuntimeError if no override is set"
    )
    assert app.dependency_overrides[oauth2_mod.get_db] is get_db, (
        "oauth2.get_db should resolve to get_db (the control-"
        "plane session factory). DCR writes go to the control DB."
    )
