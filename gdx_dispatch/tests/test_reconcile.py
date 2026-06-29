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


def test_pip_install_timeout_returns_false_not_raises(monkeypatch):
    # The network-isolated host must fail fast, not hang boot, when an index is
    # unreachable (2026-06-29 outage). A timed-out pip is a failure, not a crash.
    def _boom(*a, **k):
        raise rec.subprocess.TimeoutExpired(cmd="pip", timeout=1)

    monkeypatch.setattr(rec.subprocess, "run", _boom)
    assert rec.pip_install("foo") is False


def test_pip_install_fail_fast_flags_and_no_upgrade(monkeypatch):
    seen = {}

    def _capture(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _R(0)

    monkeypatch.setattr(rec.subprocess, "run", _capture)
    rec.pip_install("foo==1.0", target="/vol")
    assert seen["cmd"][seen["cmd"].index("--retries") + 1] == "0"
    assert "--timeout" in seen["cmd"]
    # --upgrade forces an index check the offline host can't satisfy → omitted
    assert "--upgrade" not in seen["cmd"]
    assert seen["timeout"] == rec.PIP_TIMEOUT_S


def test_artifact_name_version_parses_wheel_and_sdist():
    assert rec.artifact_name_version("gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl") == (
        "gdx_plugin_chi_pricing", "0.1.2")
    assert rec.artifact_name_version("gdx_plugin_chi_pricing-0.1.2.tar.gz") == (
        "gdx_plugin_chi_pricing", "0.1.2")
    assert rec.artifact_name_version("garbage.txt") == (None, None)


def _make_dist_info(root, dist, version):
    """Minimal installed-dist layout importlib.metadata can read from `root`."""
    d = root / f"{dist}-{version}.dist-info"
    d.mkdir()
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    return d


def test_is_installed_matches_across_name_normalization(tmp_path):
    _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", "0.1.2")
    # registry name (dashes) and wheel name (underscores) both resolve
    assert rec.is_installed("gdx-plugin-chi-pricing", "0.1.2", target=str(tmp_path))
    assert rec.is_installed("gdx_plugin_chi_pricing", "0.1.2", target=str(tmp_path))
    assert not rec.is_installed("gdx_plugin_chi_pricing", "0.1.3", target=str(tmp_path))
    assert not rec.is_installed("other", "0.1.2", target=str(tmp_path))


def test_is_installed_uses_pep440_version_equality(tmp_path):
    # pip writes the NORMALIZED version; a raw registry version must still match
    # so it isn't reinstalled (and falsely 503'd) every boot.
    _make_dist_info(tmp_path, "demo", "1.0.post1")
    assert rec.is_installed("demo", "1.0-1", target=str(tmp_path))
    _make_dist_info(tmp_path, "demo2", "1.2")
    assert rec.is_installed("demo2", "v1.2", target=str(tmp_path))


def test_is_installed_false_when_target_missing():
    assert rec.is_installed("x", "1.0", target="/no/such/dir") is False


def test_reconcile_installs_each_desired(monkeypatch):
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: False)
    monkeypatch.setattr(rec, "desired_packages", lambda db: [("foo", "1.0"), ("bar", None)])
    calls = []
    monkeypatch.setattr(rec, "pip_install", lambda spec: calls.append(spec) or True)
    out = rec.reconcile(db=object())
    assert out.installed == ["foo==1.0", "bar"]  # pinned vs unpinned spec built correctly
    assert out.failed == []
    assert calls == ["foo==1.0", "bar"]


def test_reconcile_skips_already_installed_package_without_pip(monkeypatch):
    # The volume persists across restarts: an already-present version must NOT be
    # reinstalled (reinstall re-resolves deps against an unreachable PyPI).
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "desired_packages", lambda db: [("chi", "0.1.2")])
    monkeypatch.setattr(rec, "is_installed", lambda pkg, ver, *a, **k: True)
    called = []
    monkeypatch.setattr(rec, "pip_install", lambda spec: called.append(spec) or True)
    out = rec.reconcile(db=object())
    assert called == []  # no pip invocation → no network reach
    assert out.installed == [] and out.failed == []


def test_reconcile_reports_failed_specs(monkeypatch):
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: False)
    monkeypatch.setattr(rec, "desired_packages", lambda db: [("good", None), ("bad", None)])
    monkeypatch.setattr(rec, "pip_install", lambda spec: spec == "good")
    out = rec.reconcile(db=object())
    assert out.installed == ["good"]
    assert out.failed == ["bad"]  # surfaced, not silently dropped


def test_desired_versions_merges_registry_and_artifacts(monkeypatch):
    monkeypatch.setattr(rec, "desired_packages",
                        lambda db: [("gdx-plugin-foo", "1.2"), ("nover", None)])
    monkeypatch.setattr(rec, "desired_artifact_names",
                        lambda db: ["gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl"])
    out = rec.desired_versions(db=object())
    # canonical keys; unversioned registry rows skipped; artifact version parsed
    assert out["gdx_plugin_foo"] == "1.2"
    assert out["gdx_plugin_chi_pricing"] == "0.1.2"
    assert "nover" not in out


class _M:
    def __init__(self, key):
        self.key = key


def test_detect_stale_flags_wrong_version_only():
    desired = {"gdx_plugin_chi_pricing": "0.2.0", "other": "1.0"}
    discovered = [
        (_M("chipricing"), "gdx-plugin-chi-pricing", "0.1.2"),  # 0.1.2 != 0.2.0 → stale
        (_M("other"), "other", "1.0"),                          # matches → fine
        (_M("untracked"), "untracked", "9.9"),                  # no desired → fine
    ]
    stale = rec.detect_stale(desired, discovered)
    assert stale == {"chipricing": {"installed": "0.1.2", "desired": "0.2.0"}}


def test_detect_stale_uses_pep440_equality_so_no_false_positive():
    desired = {"demo": "1.0-1"}
    discovered = [(_M("demo"), "demo", "1.0.post1")]  # same version, normalized
    assert rec.detect_stale(desired, discovered) == {}


def test_install_artifact_skips_when_already_installed(monkeypatch):
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: True)
    called = []
    monkeypatch.setattr(rec, "pip_install", lambda *a, **k: called.append(a) or True)
    ok = rec.install_artifact("gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl", b"bytes")
    assert ok is True
    assert called == []  # already present → no write, no pip, no network
