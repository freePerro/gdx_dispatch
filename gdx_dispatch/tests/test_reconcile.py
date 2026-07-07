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


def test_effective_version_is_highest_despite_accumulated_dist_info(tmp_path):
    # The prod failure: --target upgrades leave OLD dist-info behind, and the code
    # dir is whatever the LAST (highest) install wrote. effective_version must be
    # that highest version, NOT the first/oldest dist-info found.
    _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", "0.1.0")
    _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", "0.1.1")
    _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", "0.1.2")
    assert rec.installed_versions("gdx_plugin_chi_pricing", target=str(tmp_path)) == {
        "0.1.0", "0.1.1", "0.1.2"}
    assert rec.effective_version("gdx-plugin-chi-pricing", target=str(tmp_path)) == "0.1.2"
    # is_installed tracks the EFFECTIVE (running) version, not membership:
    assert rec.is_installed("gdx-plugin-chi-pricing", "0.1.2", target=str(tmp_path))
    assert not rec.is_installed("gdx-plugin-chi-pricing", "0.1.0", target=str(tmp_path))
    assert not rec.is_installed("gdx-plugin-chi-pricing", "0.9.9", target=str(tmp_path))


def test_prune_other_versions_removes_only_non_kept(tmp_path):
    for v in ("0.1.0", "0.1.1", "0.1.2"):
        _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", v)
    _make_dist_info(tmp_path, "unrelated", "9.9")  # must be untouched
    removed = rec.prune_other_versions("gdx-plugin-chi-pricing", "0.1.2", target=str(tmp_path))
    assert sorted(removed) == [
        "gdx_plugin_chi_pricing-0.1.0.dist-info", "gdx_plugin_chi_pricing-0.1.1.dist-info"]
    assert rec.installed_versions("gdx_plugin_chi_pricing", target=str(tmp_path)) == {"0.1.2"}
    assert rec.installed_versions("unrelated", target=str(tmp_path)) == {"9.9"}


def test_prune_keeps_pep440_equivalent(tmp_path):
    _make_dist_info(tmp_path, "demo", "1.0.post1")
    # keep_version given in non-normalized form must NOT delete its own dist-info
    assert rec.prune_other_versions("demo", "1.0-1", target=str(tmp_path)) == []
    assert rec.installed_versions("demo", target=str(tmp_path)) == {"1.0.post1"}


def test_prune_leaves_package_code_and_kept_metadata_intact(tmp_path):
    # Prune must not break the working plugin: the importable package dir and the
    # kept version's dist-info survive; only other-version dist-info is removed.
    pkg = tmp_path / "gdx_plugin_chi_pricing"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 42\n")
    for v in ("0.1.0", "0.1.1", "0.1.2"):
        _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", v)
    rec.prune_other_versions("gdx_plugin_chi_pricing", "0.1.2", target=str(tmp_path))
    assert (pkg / "__init__.py").read_text() == "VALUE = 42\n"          # code untouched
    assert (tmp_path / "gdx_plugin_chi_pricing-0.1.2.dist-info").is_dir()  # kept metadata
    assert rec.installed_versions("gdx_plugin_chi_pricing", target=str(tmp_path)) == {"0.1.2"}


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


def test_reconcile_skips_filename_row_when_already_installed(monkeypatch):
    # Issue #100: an operator pasted a wheel FILENAME into the registry package
    # field (empty version). It must NOT become `pip install <bare filename>`
    # (which fails every boot and wedges /ready). Since the volume already has the
    # dist, reconcile resolves the filename and skips — no pip, nothing failed.
    fn = "gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl"
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "desired_packages", lambda db: [(fn, "")])
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: True)
    monkeypatch.setattr(rec, "prune_other_versions", lambda *a, **k: [])
    called = []
    monkeypatch.setattr(rec, "pip_install", lambda spec: called.append(spec) or True)
    out = rec.reconcile(db=object())
    assert called == []                       # never pip-installed a bare filename
    assert out.installed == [] and out.failed == []  # not reported as a failed spec


def test_reconcile_filename_row_not_installed_is_skipped_not_failed(monkeypatch):
    # Same bad row, dist NOT on the volume. DELIBERATE tradeoff: a filename row is
    # operator cruft (the artifact installer is the real path for uploaded wheels),
    # so it must NOT gate /ready — we skip + log a warning rather than pip-install a
    # bare filename (which would wedge red) or mark it failed. The add_plugin guard
    # prevents such rows being created in the first place; this only handles legacy
    # rows. NOT silently pretending success — it's logged and the row is ignored.
    fn = "gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl"
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "desired_packages", lambda db: [(fn, "")])
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(rec, "pip_install", lambda spec: called.append(spec) or True)
    out = rec.reconcile(db=object())
    assert called == []
    assert out.failed == []


def test_reconcile_filename_row_version_mismatch_is_skipped_not_pip(monkeypatch):
    # Filename parses but its version != the effective installed version → the
    # is_installed branch is False, so we still skip (log) instead of pip-installing
    # the bare filename. Documents the audit's version-skew edge: skip, never wedge.
    fn = "gdx_plugin_chi_pricing-9.9.9-py3-none-any.whl"  # installed is 0.1.2, say
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "desired_packages", lambda db: [(fn, "")])
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(rec, "pip_install", lambda spec: called.append(spec) or True)
    out = rec.reconcile(db=object())
    assert called == [] and out.failed == []


def test_reconcile_filename_bypasses_are_caught(monkeypatch):
    # The audit's bypass inputs (uppercase ext, trailing space, empty-version
    # `foo-.whl`, .zip) must ALL be recognized as filenames and skipped — never
    # reach `pip install <bare filename>` and wedge /ready.
    rows = ["Plugin.WHL", "gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl ",
            "foo-.whl", "some-plugin.zip"]
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "desired_packages", lambda db: [(r, "") for r in rows])
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(rec, "pip_install", lambda spec: called.append(spec) or True)
    out = rec.reconcile(db=object())
    assert called == []       # not one bare filename reached pip
    assert out.failed == []


def test_looks_like_artifact_filename_classifies():
    f = rec.looks_like_artifact_filename
    for good in ("x-1.0.whl", "x-1.0.tar.gz", "X-1.0.WHL", " x-1.0.whl ",
                 "foo-.whl", "a.zip", "a.tgz", "a.egg", "dir/x.whl", "..\\x.whl"):
        assert f(good), good
    for pkg in ("gdx-plugin-example", "requests", "gdx_plugin_chi_pricing",
                "", None, "numpy==1.2"):
        assert not f(pkg), pkg


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


def test_detect_stale_flags_when_desired_version_absent_from_volume(tmp_path):
    # detect_stale reads the VOLUME (membership), not the entry point's version.
    _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", "0.1.2")  # only 0.1.2 present
    _make_dist_info(tmp_path, "other", "1.0")
    desired = {"gdx_plugin_chi_pricing": "0.2.0", "other": "1.0"}
    discovered = [
        (_M("chipricing"), "gdx-plugin-chi-pricing", "0.1.2"),  # desired 0.2.0 absent → stale
        (_M("other"), "other", "1.0"),                          # 1.0 present → fine
        (_M("untracked"), "untracked", "9.9"),                  # no desired → fine
    ]
    stale = rec.detect_stale(desired, discovered, target=str(tmp_path))
    assert stale == {"chipricing": {"installed": "0.1.2", "desired": "0.2.0"}}


def test_detect_stale_not_flagged_when_desired_is_effective_despite_cruft(tmp_path):
    # The exact prod false-positive: 0.1.0/0.1.1/0.1.2 all present, desired 0.1.2.
    # effective (highest) == desired → NOT stale, even though the entry point below
    # reports the oldest version. This is the regression test for the v1.5.1 bug.
    for v in ("0.1.0", "0.1.1", "0.1.2"):
        _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", v)
    desired = {"gdx_plugin_chi_pricing": "0.1.2"}
    discovered = [(_M("chipricing"), "gdx-plugin-chi-pricing", "0.1.0")]  # ep reports oldest
    assert rec.detect_stale(desired, discovered, target=str(tmp_path)) == {}


def test_detect_stale_uses_pep440_equality_so_no_false_positive(tmp_path):
    _make_dist_info(tmp_path, "demo", "1.0.post1")
    desired = {"demo": "1.0-1"}  # same version, non-normalized
    discovered = [(_M("demo"), "demo", "1.0.post1")]
    assert rec.detect_stale(desired, discovered, target=str(tmp_path)) == {}


def test_install_artifact_skips_when_already_installed_and_prunes(monkeypatch):
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: True)
    called = []
    pruned = []
    monkeypatch.setattr(rec, "pip_install", lambda *a, **k: called.append(a) or True)
    monkeypatch.setattr(rec, "prune_other_versions",
                        lambda d, v, target=rec.INSTALL_DIR: pruned.append((d, v)))
    ok = rec.install_artifact("gdx_plugin_chi_pricing-0.1.2-py3-none-any.whl", b"bytes")
    assert ok is True
    assert called == []  # already present → no write, no pip, no network
    assert pruned == [("gdx_plugin_chi_pricing", "0.1.2")]  # cruft pruned on skip
