"""Tests for plugin reconcile (ADR-013 step 5): pip-install result handling and
that reconcile installs each desired package. subprocess is mocked. Imports
core.database → runs in the docker image.
"""
from __future__ import annotations

from gdx_dispatch.plugin_host import reconcile as rec


class _R:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_pip_install_success(monkeypatch):
    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: _R(0))
    assert rec.pip_install("foo==1.0") is True


def test_pip_install_failure_returns_false_not_raises(monkeypatch):
    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: _R(1, "boom"))
    assert rec.pip_install("foo") is False


def test_reconcile_installs_each_desired(monkeypatch):
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "desired_packages", lambda db: [("foo", "1.0"), ("bar", None)])
    calls = []
    monkeypatch.setattr(rec, "pip_install", lambda spec: calls.append(spec) or True)
    out = rec.reconcile(db=object())
    assert out == ["foo==1.0", "bar"]  # version pinned vs unpinned spec built correctly
    assert calls == ["foo==1.0", "bar"]


def test_reconcile_skips_failed_install(monkeypatch):
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "desired_packages", lambda db: [("good", None), ("bad", None)])
    monkeypatch.setattr(rec, "pip_install", lambda spec: spec == "good")
    out = rec.reconcile(db=object())
    assert out == ["good"]  # 'bad' failed install → not in result, no raise
