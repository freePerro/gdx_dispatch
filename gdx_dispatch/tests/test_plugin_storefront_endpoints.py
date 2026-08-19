"""The storefront endpoints on the plugin admin router.

The load-bearing assertion here is a NEGATIVE one: a store install must write
`plugin_artifact` and **never** `plugin_registry`. A registry row naming an
index package makes plugin-host try to `pip install` it from PyPI on its next
boot; with no egress that fails, the plugin lands in `degraded`, `/ready`
returns 503 and the container flips unhealthy — at exactly the moment the owner
expects their new plugin to appear.

Runs in the docker image (imports the router).
"""
from __future__ import annotations

import hashlib

import pytest

from gdx_dispatch.core import plugin_storefront as store
from gdx_dispatch.routers import admin_plugins as ap

WHEEL = b"PK\x03\x04 pretend wheel"
DIGEST = hashlib.sha256(WHEEL).hexdigest()
ENTRY = {
    "key": "n8n", "distribution": "gdx-plugin-n8n", "version": "0.1.0",
    "name": "n8n Automations", "description": "", "author": "", "tier": "starter",
    "permissions": ["events"], "requires": "", "license": "free",
    "wheel_url": "https://github.com/o/r/releases/download/v0.1.0/gdx_plugin_n8n-0.1.0-py3-none-any.whl",
    "sha256": DIGEST, "size": len(WHEEL),
}


class _DB:
    """Records every statement so we can assert which TABLES were written."""

    def __init__(self):
        self.sql: list[str] = []

    def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        return None

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(ap, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(ap, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(ap, "_audit", lambda *a, **k: None)
    return _DB()


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(store, "find_entry",
                        lambda key, version=None: dict(ENTRY) if key == ENTRY["key"]
                        else (_ for _ in ()).throw(store.StorefrontError(f"{key!r} is not in the plugin catalog")))
    monkeypatch.setattr(store, "download_wheel",
                        lambda entry: ("gdx_plugin_n8n-0.1.0-py3-none-any.whl", WHEEL))
    store.reset_cache()


def _install(db, key="n8n", version="0.1.0"):
    return ap.install_from_storefront(
        ap.StorefrontInstall(key=key, version=version),
        request=None,
        user={"role": "owner", "sub": "u1"},
        db=db,
    )


def test_installing_writes_the_artifact_and_never_the_registry(db):
    out = _install(db)

    joined = " ".join(db.sql).lower()
    assert "insert into plugin_artifact" in joined
    assert "plugin_registry" not in joined, (
        "a storefront install wrote a plugin_registry row — plugin-host would try to "
        "pip-install it from an index it cannot reach and go unhealthy on the next boot"
    )
    assert out["status"] == "pending_restart"
    assert out["sha256"] == DIGEST


def test_the_response_never_claims_the_plugin_is_running(db):
    """Recorded is not running. The plugin only loads on the next restart, and
    saying otherwise is the fake-success class this repo treats as severe."""
    out = _install(db)
    assert out["status"] == "pending_restart"
    assert "restart" in out["note"].lower()
    assert "installed" not in out["status"].lower()


def test_the_install_is_audited_with_what_was_installed(db, monkeypatch):
    seen = {}

    def _capture(db_, request, user, action, **kw):
        seen.update({"action": action, **kw})

    monkeypatch.setattr(ap, "_audit", _capture)
    _install(db)

    assert seen["action"] == "plugin.storefront_installed"
    assert seen["entity_id"] == "n8n"
    d = seen["details"]
    assert d["version"] == "0.1.0" and d["sha256"] == DIGEST
    assert d["distribution"] == "gdx-plugin-n8n"
    # The permissions the owner accepted are part of the record.
    assert d["permissions"] == ["events"]


def test_the_uploader_is_recorded_as_the_storefront_and_the_user(db):
    _install(db)
    # Distinguishing a store install from a hand upload matters when someone
    # later asks where a plugin came from.
    assert any("storefront:" in s or "uploaded_by" in s.lower() for s in db.sql)


def test_a_plugin_not_in_the_catalog_is_refused(db):
    with pytest.raises(ap.HTTPException) as e:
        _install(db, key="not-listed")
    assert e.value.status_code == 502
    assert "catalog" in str(e.value.detail)
    assert db.sql == [], "nothing may be written for a plugin that is not listed"


def test_a_download_failure_writes_nothing(db, monkeypatch):
    def _boom(entry):
        raise store.StorefrontError("integrity check failed")

    monkeypatch.setattr(store, "download_wheel", _boom)

    with pytest.raises(ap.HTTPException) as e:
        _install(db)
    assert e.value.status_code == 502
    assert db.sql == [], "a failed download must not leave a partial install"


def test_browsing_reports_an_unreachable_catalog_instead_of_an_empty_store(db, monkeypatch):
    """An empty list would read as "there are no plugins", which is a different
    and wrong statement."""
    monkeypatch.setattr(store, "fetch_catalog",
                        lambda *a, **k: (_ for _ in ()).throw(store.StorefrontError("boom")))

    out = ap.browse_storefront(db=db)

    assert out["plugins"] == []
    assert out["error"] and "boom" in out["error"]
    assert out["catalog_url"]


def test_browsing_merges_install_state(db, monkeypatch):
    monkeypatch.setattr(store, "fetch_catalog", lambda *a, **k: [dict(ENTRY)])
    monkeypatch.setattr(ap, "_desired_versions", lambda db_: {"gdx_plugin_n8n": "0.1.0"})
    monkeypatch.setattr(ap, "_running_versions", lambda: {"gdx_plugin_n8n": "0.1.0"})

    out = ap.browse_storefront(db=db)

    assert out["error"] is None
    [item] = out["plugins"]
    assert item["state"] == "running"
    assert item["permissions"] == ["events"]


def test_browsing_survives_plugin_host_being_down(db, monkeypatch):
    """plugin-host restarts constantly during installs; the store must render."""
    monkeypatch.setattr(store, "fetch_catalog", lambda *a, **k: [dict(ENTRY)])
    monkeypatch.setattr(ap, "_desired_versions", lambda db_: {})
    monkeypatch.setattr(ap.httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection refused")))

    out = ap.browse_storefront(db=db)

    [item] = out["plugins"]
    assert item["running_version"] is None
    assert item["state"] == "available"


def test_the_storefront_routes_are_owner_only():
    """`admin` is deliberately excluded — the same gate as every other route
    here (an admin would see the store and 403 on install)."""
    routes = {r.path: r for r in ap.router.routes if hasattr(r, "path")}
    for path in ("/api/admin/plugins/storefront", "/api/admin/plugins/storefront/install"):
        assert path in routes, f"{path} is not registered"
        deps = str(routes[path].dependant.dependencies)
        assert "_require_owner" in deps or any(
            "_require_owner" in str(d.call) for d in routes[path].dependant.dependencies
        ), f"{path} is not owner-gated"
