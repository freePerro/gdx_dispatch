"""The segments page's bulk "Add Tag" must not fake success.

``routers/sub_resources.py`` used to mount ``POST /api/customers/bulk-tag`` and
answer ``{"ok": True, "tagged": N}`` without writing anything. The office saw
"tagged" and nothing changed. Decision (WO-005, D1): remove the affordance and
the fake-success handler rather than build it. The ``ui_compat`` shim for the
same path stays and refuses with a logged 501, so anything still posting there
fails loud instead of silently succeeding.
"""
from __future__ import annotations

from pathlib import Path

from gdx_dispatch.routers import sub_resources

_VUE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "views" / "SegmentsView.vue"


def test_sub_resources_no_longer_mounts_bulk_tag():
    assert not hasattr(sub_resources, "customers_bulk_tag")
    paths = {getattr(r, "path", None) for r in sub_resources.router.routes}
    assert "/api/customers/bulk-tag" not in paths


def test_live_bulk_tag_route_refuses_instead_of_faking_success():
    from gdx_dispatch.app import app
    from gdx_dispatch.core.auth import get_current_user
    from gdx_dispatch.core.modules import require_module

    # ui_compat is gated by require_module("jobs"); the gate is lru_cached, so
    # the same callable is the override key (see test_payments_portal_authz).
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": "t-1",
    }
    app.dependency_overrides[require_module("jobs")] = lambda: None
    try:
        from fastapi.testclient import TestClient

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/api/customers/bulk-tag", json={"customer_ids": ["c-1"], "tag": "vip"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(require_module("jobs"), None)
    assert resp.status_code == 501, resp.text
    assert resp.json().get("ok") is not True


def test_segments_view_has_no_add_tag_affordance():
    src = _VUE.read_text(encoding="utf-8")
    for needle in ("bulk-tag", "bulkTag", "BulkTag", "Add Tag"):
        assert needle not in src, needle
