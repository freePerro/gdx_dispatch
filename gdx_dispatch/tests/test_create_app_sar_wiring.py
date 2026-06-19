"""Regression: create_app() must wire the SAR router's get_db sentinel
to the real control-plane session factory.

2026-05-09/10: GET /api/sar/{id}/download 500'd "no db session on
request" (×3 in CC support/errors, all schemathesis contract-fuzz, no
real users). The SS-35 SAR router read `request.state.db`, which no
prod middleware sets, so every /api/sar/* call failed.
test_sar_router.py masked it by injecting request.state.db in test
middleware — the prod-parity gap feedback_test_prod_token_parity.md
warns about.

Fix mirrored the SS-21 oauth2 / SS-31 federation pattern: a get_db
sentinel overridden in create_app(). This asserts the override exists
so the bug can't re-appear silently — sibling of
test_create_app_oauth_wiring.py.

Note: this pins the download/status wiring only. SAR *filing*
(request_sar) is separately fenced (501) until the SECURITY DEFINER
cross-tenant gather lands — see D-ss35-sar-integration and
sar._sar_build_ready().
"""
from __future__ import annotations

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers import sar as sar_mod


def test_create_app_overrides_sar_get_db():
    """The SAR get_db sentinel must resolve to get_db after
    create_app(). download/status only touch `sar_request` (RLS OFF on
    prod), so the control-plane factory serves them correctly with no
    tenant GUC. Imported lazily so engine/lifespan construction doesn't
    fire at collection time.
    """
    from gdx_dispatch.app import create_app

    app = create_app()

    assert sar_mod.get_db in app.dependency_overrides, (
        "create_app() must override sar.get_db; the router declares a "
        "sentinel that raises RuntimeError if no override is set"
    )
    assert app.dependency_overrides[sar_mod.get_db] is get_db, (
        "sar.get_db should resolve to get_db (the control-plane "
        "session factory). sar_request lives in the control DB."
    )
