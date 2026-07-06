"""add_plugin filename guard (issue #100).

The install form's Package field is free text. Operators sometimes paste a wheel
FILENAME into it; recorded verbatim it makes reconcile try `pip install <bare
filename>` every boot and wedges plugin-host /ready red. add_plugin must refuse
to store a filename as a package: succeed as a no-op if it's already uploaded,
else 400 → Upload flow. A real package name still inserts normally.

Imports the router module (core.database etc.) → runs in the docker image.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")

import pytest

from gdx_dispatch.routers import admin_plugins as ap

_WHEEL = "gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl"


class _DB:
    """Records execute() calls so we can assert whether a row was written."""

    def __init__(self):
        self.executed = []

    def execute(self, *a, **k):
        self.executed.append(a)
        return None

    def commit(self):
        pass


def _no_tables(monkeypatch):
    monkeypatch.setattr(ap, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(ap, "ensure_artifact_table", lambda db: None)


def test_filename_not_uploaded_is_rejected_without_writing_a_row(monkeypatch):
    _no_tables(monkeypatch)
    monkeypatch.setattr(ap, "desired_artifact_names", lambda db: [])
    db = _DB()
    with pytest.raises(ap.HTTPException) as e:
        ap.add_plugin(ap.PluginInstall(package=_WHEEL), user={"role": "owner"}, db=db)
    assert e.value.status_code == 400
    assert db.executed == []  # never inserted the bogus registry row


def test_filename_already_uploaded_is_noop_success(monkeypatch):
    _no_tables(monkeypatch)
    monkeypatch.setattr(ap, "desired_artifact_names", lambda db: [_WHEEL])
    db = _DB()
    out = ap.add_plugin(ap.PluginInstall(package=_WHEEL), user={"role": "owner"}, db=db)
    assert out["status"] == "already-uploaded"
    assert out["version"] == "0.1.2"  # parsed from the filename
    assert db.executed == []  # no INSERT — the artifact already installs it


@pytest.mark.parametrize("bad", ["Plugin.WHL", "some-plugin.zip", "foo.tar.gz", "x.egg"])
def test_filename_bypass_variants_are_rejected(monkeypatch, bad):
    # Audit follow-up: uppercase ext / .zip / .egg must not slip past the guard and
    # get stored as a package (which would wedge reconcile).
    _no_tables(monkeypatch)
    monkeypatch.setattr(ap, "desired_artifact_names", lambda db: [])
    db = _DB()
    with pytest.raises(ap.HTTPException) as e:
        ap.add_plugin(ap.PluginInstall(package=bad), user={"role": "owner"}, db=db)
    assert e.value.status_code == 400
    assert db.executed == []


def test_real_package_name_still_inserts(monkeypatch):
    _no_tables(monkeypatch)
    monkeypatch.setattr(ap, "desired_artifact_names", lambda db: [])
    db = _DB()
    out = ap.add_plugin(
        ap.PluginInstall(package="gdx-plugin-example", version="1.0"),
        user={"role": "owner", "sub": "u1"},
        db=db,
    )
    assert out["status"] == "registered"
    assert len(db.executed) == 1  # the INSERT ran for a genuine package name
