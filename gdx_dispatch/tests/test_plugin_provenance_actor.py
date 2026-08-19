"""The plugin tables record WHO, for every shape of authenticated principal.

`plugin_artifact.uploaded_by`, `plugin_registry.added_by` and
`plugin_consent.consented_by` are provenance columns — they answer "who put this
here" for code that runs with backend access. They read the principal straight
off `user.get("sub")`, which is empty for a token that carries it as `user_id`.
On a real owner session that meant the columns stored nothing while the audit
row, which resolves sub/user_id/id, named the right person: two records of the
same action disagreeing.

Caught on production after a real install, not by the suite — every test here
had been passing `{"sub": ...}`, the one shape that happened to work. These
tests use the other shapes.

Runs in the docker image (imports the router).
"""
from __future__ import annotations

import pytest

from gdx_dispatch.routers import admin_plugins as ap

#: The principal shapes `get_current_user` actually hands a handler.
PRINCIPALS = [
    pytest.param({"sub": "sub-1", "role": "owner"}, "sub-1", id="sub"),
    pytest.param({"user_id": "uid-2", "role": "owner"}, "uid-2", id="user_id"),
    pytest.param({"id": "id-3", "role": "owner"}, "id-3", id="id"),
    pytest.param({"user_id": "uid-4", "sub": "sub-4", "role": "owner"}, "sub-4", id="both"),
]


@pytest.mark.parametrize(("principal", "expected"), PRINCIPALS)
def test_the_actor_resolves_for_every_principal_shape(principal, expected):
    assert ap._actor(principal) == expected


def test_an_unresolvable_principal_is_marked_not_blanked():
    """A blank provenance column reads as "nobody" — which is a claim, and a
    false one. "system" at least says the lookup found no human."""
    assert ap._actor({"role": "owner"}) == "system"


class _DB:
    def __init__(self):
        self.params: list = []

    def execute(self, stmt, params=None):
        if params:
            self.params.append(params)
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
    monkeypatch.setattr(ap, "looks_like_artifact_filename", lambda p: False)
    return _DB()


def test_registering_a_package_records_a_user_id_principal(db):
    ap.add_plugin(
        ap.PluginInstall(package="gdx-plugin-example", version="1.0"),
        request=None,
        user={"user_id": "uid-2", "role": "owner"},
        db=db,
    )
    assert db.params and db.params[-1]["by"] == "uid-2", (
        "added_by was blank for a principal carrying user_id rather than sub"
    )


def test_a_storefront_install_records_the_installer(db, monkeypatch):
    import hashlib

    from gdx_dispatch.core import plugin_storefront as store

    wheel = b"PK\x03\x04 wheel"
    entry = {
        "key": "n8n", "distribution": "gdx-plugin-n8n", "version": "0.1.0",
        "name": "n8n", "permissions": ["events"],
        "wheel_url": "https://github.com/o/r/gdx_plugin_n8n-0.1.0-py3-none-any.whl",
        "sha256": hashlib.sha256(wheel).hexdigest(),
    }
    monkeypatch.setattr(store, "find_entry", lambda k, v=None: dict(entry))
    monkeypatch.setattr(store, "download_wheel",
                        lambda e: ("gdx_plugin_n8n-0.1.0-py3-none-any.whl", wheel))

    ap.install_from_storefront(
        ap.StorefrontInstall(key="n8n", version="0.1.0"),
        request=None,
        user={"user_id": "uid-2", "role": "owner"},
        db=db,
    )

    # Tagged as a store install AND attributed — "storefront:" with nothing
    # after it is what production actually recorded before this fix.
    assert db.params[-1]["by"] == "storefront:uid-2"


def test_consent_records_who_granted_it(db, monkeypatch):
    granted = {}
    monkeypatch.setattr(ap, "fetch_permissions", lambda key: ["events"])
    monkeypatch.setattr(ap, "record_consent",
                        lambda db_, key, perms, by, commit=True: granted.update(by=by))

    ap.consent_plugin("n8n", request=None,
                      user={"user_id": "uid-2", "role": "owner"}, db=db)

    # ADR-014's whole point is that an owner consented to elevated permissions;
    # an unattributed grant does not carry that.
    assert granted["by"] == "uid-2"
