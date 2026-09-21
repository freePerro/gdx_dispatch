"""Tests for plugin reconcile (ADR-013 step 5): pip-install result handling and
that reconcile installs each desired package. subprocess is mocked. Imports
core.database → runs in the docker image.
"""
from __future__ import annotations

import time

import pytest

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


# --- removal of plugins no longer desired (2026-09-21) -------------------------
#
# Until this landed, reconcile installed and version-pruned what was desired but
# never subtracted what was not: DELETE on an artifact or a registry row left the
# package on the persistent volume, discovery kept loading its entry point, and
# the plugin ran on while the admin page said it was gone.

def _make_installed_plugin(root, dist, version, *, plugin=True):
    """A dist-info + package dir laid out the way pip --target leaves them,
    with a RECORD naming both (remove_installed_dist trusts RECORD) and, for a
    plugin, an entry_points.txt declaring the gdx.modules group."""
    info = _make_dist_info(root, dist, version)
    pkg = root / dist
    pkg.mkdir()
    (pkg / "__init__.py").write_text("manifest = object()\n")
    lines = [f"{dist}/__init__.py,,", f"{info.name}/METADATA,,", f"{info.name}/RECORD,,"]
    if plugin:
        (info / "entry_points.txt").write_text(f"[gdx.modules]\n{dist} = {dist}:manifest\n")
        lines.append(f"{info.name}/entry_points.txt,,")
    (info / "RECORD").write_text("\n".join(lines) + "\n")
    return info, pkg


def _intent(at, version=None, by="owner"):
    """A recorded removal for tests: an artifact deletion when `version` is set
    (keyed by that version), a registry unregister otherwise (key None)."""
    if version is None:
        return rec.RemovalIntent(at, "plugin.unregistered", "pkg", by)
    return rec.RemovalIntent(at, "plugin.artifact_deleted", f"pkg-{version}-py3-none-any.whl", by)


def _reconcile_with(monkeypatch, tmp_path, *, packages=(), artifacts=(), intents=None, install_artifact=None):
    """Drive reconcile() against a tmp volume. `intents` = what the audit trail
    says the operator removed ({canonical name: epoch}); the default records a
    removal of everything a moment from now, so tests read as "removed via the
    DELETE endpoint just before this boot"."""
    if intents is None:  # "unregistered a moment from now" for everything on the volume
        intents = {c: {None: _intent(time.time() + 60)} for c in rec.plugin_dists_on_volume(str(tmp_path))}
    monkeypatch.setattr(rec, "INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(rec, "recorded_removals", lambda db: dict(intents))
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_packages", lambda db: list(packages))
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: list(artifacts))
    monkeypatch.setattr(rec, "is_installed", lambda *a, **k: True)
    monkeypatch.setattr(rec, "pip_install", lambda *a, **k: True)
    monkeypatch.setattr(rec, "install_artifact", install_artifact or (lambda *a, **k: True))
    monkeypatch.setattr(rec, "db_is_the_apps", lambda db: True)
    audited.clear()
    monkeypatch.setattr(rec, "_audit_removals", lambda db, names, applied: audited.extend(names))
    return rec.reconcile(db=object())


#: What `_reconcile_with` saw the audit writer receive (names of removed plugins).
audited: list[str] = []


def test_plugin_dists_on_volume_sees_plugins_only(tmp_path):
    _make_installed_plugin(tmp_path, "demoplug", "1.0")
    _make_installed_plugin(tmp_path, "somelib", "2.3", plugin=False)  # a library, no gdx.modules
    (tmp_path / "junk.dist-info").mkdir()  # no entry_points.txt at all
    assert rec.plugin_dists_on_volume(str(tmp_path)) == {"demoplug": "demoplug"}


def test_reconcile_removes_a_plugin_nobody_desires_any_more(tmp_path, monkeypatch):
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    lib_info, lib_pkg = _make_installed_plugin(tmp_path, "somelib", "2.3", plugin=False)

    out = _reconcile_with(monkeypatch, tmp_path)  # registry empty, no artifacts

    assert out.removed == ("demoplug",)
    assert not pkg.exists() and not info.exists()  # code AND metadata gone → nothing to discover
    assert lib_pkg.exists() and lib_info.exists()  # a library on the volume is never a candidate


def test_reconcile_keeps_a_plugin_that_an_artifact_row_still_desires(tmp_path, monkeypatch):
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    out = _reconcile_with(monkeypatch, tmp_path,
                          artifacts=[("demoplug-1.0-py3-none-any.whl", "sha", b"")])
    assert out.removed == ()
    assert pkg.exists() and info.exists()


def test_reconcile_keeps_a_plugin_that_a_registry_row_still_desires(tmp_path, monkeypatch):
    # Name normalisation: the registry says gdx-plugin-demo, the volume has
    # gdx_plugin_demo. Same distribution → kept.
    info, pkg = _make_installed_plugin(tmp_path, "gdx_plugin_demo", "0.4.0")
    out = _reconcile_with(monkeypatch, tmp_path, packages=[("gdx-plugin-demo", "0.4.0")])
    assert out.removed == ()
    assert pkg.exists() and info.exists()


def test_reconcile_keeps_a_desired_plugin_even_when_its_install_failed(tmp_path, monkeypatch):
    # A failed reinstall is retried next boot; it must not be read as "removed".
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    monkeypatch.setattr(rec, "install_artifact", lambda *a, **k: False)
    monkeypatch.setattr(rec, "db_is_the_apps", lambda db: True)
    monkeypatch.setattr(rec, "INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_packages", lambda db: [])
    monkeypatch.setattr(rec, "desired_artifacts",
                        lambda db: [("demoplug-1.1-py3-none-any.whl", "sha", b"")])
    monkeypatch.setattr(rec, "recorded_removals", lambda db: {"demoplug": {None: _intent(time.time() + 60)}})
    out = rec.reconcile(db=object())
    assert out.failed == ["demoplug-1.1-py3-none-any.whl"]
    assert out.removed == ()
    assert pkg.exists()


def test_reconcile_removes_nothing_when_desired_state_cannot_be_read(tmp_path, monkeypatch):
    # "Not desired" is only a fact after both reads succeed. A DB blip must not
    # turn into a purge of every installed plugin.
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    monkeypatch.setattr(rec, "INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_packages", lambda db: [])

    def _db_down(db):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(rec, "desired_artifacts", _db_down)
    with pytest.raises(RuntimeError):
        rec.reconcile(db=object())
    assert pkg.exists() and info.exists()


@pytest.mark.parametrize("spec", ["demoplug[browser]", "demoplug>=1.0", "demoplug == 1.0", "Demo-Plug"])
def test_a_registry_spec_with_extras_or_a_specifier_still_names_its_plugin(tmp_path, monkeypatch, spec):
    # Before the removal step existed, such a row merely reinstalled every boot;
    # with it, misreading the name would install and delete the same plugin in
    # one pass. The distribution name is parsed out of the spec.
    dist = "demo_plug" if spec == "Demo-Plug" else "demoplug"
    info, pkg = _make_installed_plugin(tmp_path, dist, "1.0")
    out = _reconcile_with(monkeypatch, tmp_path, packages=[(spec, None)])
    assert out.removed == ()
    assert pkg.exists() and info.exists()


def test_spec_distribution_parses_the_name_only():
    assert rec.spec_distribution("demoplug[browser]==1.0") == "demoplug"
    assert rec.spec_distribution("  gdx-plugin-chi-pricing >= 0.4") == "gdx-plugin-chi-pricing"
    assert rec.spec_distribution("") is None


def test_reconcile_removes_nothing_from_a_database_that_is_not_the_apps(tmp_path, monkeypatch):
    # The SQLite fallback (DATABASE_URL unset) or any empty DB answers the desired-
    # state queries with zero rows. Zero rows must never read as a purge order.
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    monkeypatch.setattr(rec, "INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_packages", lambda db: [])
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "db_is_the_apps", lambda db: False)
    monkeypatch.setattr(rec, "recorded_removals", lambda db: {"demoplug": {None: _intent(time.time() + 60)}})
    out = rec.reconcile(db=object())
    assert out.removed == ()
    assert pkg.exists() and info.exists()


def test_db_is_the_apps_needs_a_migrated_schema():
    class _Result:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _DB:
        def __init__(self, row=None, raises=False):
            self._row, self._raises, self.rolled_back = row, raises, False

        def execute(self, *a, **k):
            if self._raises:
                raise RuntimeError("no such table: alembic_version")
            return _Result(self._row)

        def rollback(self):
            self.rolled_back = True

    assert rec.db_is_the_apps(_DB(row=("096",))) is True
    assert rec.db_is_the_apps(_DB(row=None)) is False
    bad = _DB(raises=True)
    assert rec.db_is_the_apps(bad) is False and bad.rolled_back is True


def test_removing_a_plugin_also_drops_its_staged_wheels(tmp_path, monkeypatch):
    _make_installed_plugin(tmp_path, "demoplug", "1.0")
    _make_installed_plugin(tmp_path, "otherplug", "1.0")
    staged = tmp_path / "_artifacts"
    staged.mkdir()
    (staged / "demoplug-0.9-py3-none-any.whl").write_bytes(b"old")
    (staged / "demoplug-1.0-py3-none-any.whl").write_bytes(b"new")
    (staged / "otherplug-1.0-py3-none-any.whl").write_bytes(b"keep")
    # otherplug is still desired (its artifact row survives); demoplug 1.0's row
    # was deleted. Only the wheel that row named goes: the 0.9 wheel nobody named
    # stays (prod keeps seven chi_pricing wheels the database knows two of).
    out = _reconcile_with(monkeypatch, tmp_path, artifacts=[("otherplug-1.0-py3-none-any.whl", "sha", b"")],
                          intents={"demoplug": {"1.0": _intent(time.time() + 60, "1.0")}})
    assert out.removed == ("demoplug",)
    assert sorted(p.name for p in staged.iterdir()) == ["demoplug-0.9-py3-none-any.whl",
                                                         "otherplug-1.0-py3-none-any.whl"]
    assert (tmp_path / "otherplug").exists()


def test_an_unregister_drops_every_staged_wheel_of_the_package(tmp_path, monkeypatch):
    _make_installed_plugin(tmp_path, "demoplug", "1.0")
    staged = tmp_path / "_artifacts"
    staged.mkdir()
    (staged / "demoplug-0.9-py3-none-any.whl").write_bytes(b"old")
    (staged / "demoplug-1.0-py3-none-any.whl").write_bytes(b"new")
    out = _reconcile_with(monkeypatch, tmp_path)  # default: unregistered
    assert out.removed == ("demoplug",)
    assert list(staged.iterdir()) == []


def test_every_removal_is_offered_to_the_audit_trail(tmp_path, monkeypatch):
    _make_installed_plugin(tmp_path, "demoplug", "1.0")
    _make_installed_plugin(tmp_path, "otherplug", "2.0")
    out = _reconcile_with(monkeypatch, tmp_path)
    assert out.removed == ("demoplug", "otherplug")
    assert sorted(audited) == ["demoplug", "otherplug"]


def test_a_failed_audit_write_never_aborts_the_boot(tmp_path, monkeypatch, caplog):
    _make_installed_plugin(tmp_path, "demoplug", "1.0")
    monkeypatch.setattr(rec, "INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(rec, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rec, "ensure_artifact_table", lambda db: None)
    monkeypatch.setattr(rec, "desired_packages", lambda db: [])
    monkeypatch.setattr(rec, "desired_artifacts", lambda db: [])
    monkeypatch.setattr(rec, "db_is_the_apps", lambda db: True)
    monkeypatch.setattr(rec, "recorded_removals", lambda db: {"demoplug": {None: _intent(time.time() + 60)}})
    # db=object(): log_audit_event_sync cannot use it → the writer must log and move on.
    out = rec.reconcile(db=object())
    assert out.removed == ("demoplug",)
    assert any("could not write audit row" in r.getMessage() for r in caplog.records)


def test_no_recorded_removal_means_no_removal(tmp_path, monkeypatch):
    # The local dev stack, 2026-09-21: plugins on the volume, both desired-state
    # tables empty, alembic_version present. Absence is not intent.
    info, pkg = _make_installed_plugin(tmp_path, "gdx_plugin_chi_pricing", "0.1.1")
    out = _reconcile_with(monkeypatch, tmp_path, intents={})
    assert out.removed == ()
    assert pkg.exists() and info.exists()


def test_a_removal_recorded_before_this_copy_was_installed_does_not_apply(tmp_path, monkeypatch):
    # Deleted via the UI last month, put back by hand since: the old intent is spent.
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    out = _reconcile_with(monkeypatch, tmp_path, intents={"demoplug": {None: _intent(time.time() - 3600)}})
    assert out.removed == ()
    assert pkg.exists() and info.exists()


def test_a_desired_row_wins_over_a_recorded_removal(tmp_path, monkeypatch):
    # Deleted, then re-uploaded: the newer artifact row is desired state; the
    # earlier deletion must not remove the reinstalled copy.
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    out = _reconcile_with(monkeypatch, tmp_path,
                          artifacts=[("demoplug-1.0-py3-none-any.whl", "sha", b"")],
                          intents={"demoplug": {"1.0": _intent(time.time() + 60, "1.0")}})
    assert out.removed == ()
    assert pkg.exists()


def test_recorded_removals_reads_both_delete_actions_and_keeps_the_latest():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(tz=UTC)

    class _DB:
        def execute(self, *a, **k):
            rows = [  # the later 0.1.0 row comes FIRST: last-write-wins would get it wrong
                ("plugin.artifact_deleted", "gdx_plugin_demo-0.1.0-py3-none-any.whl", now - timedelta(days=2), "a"),
                ("plugin.artifact_deleted", "gdx_plugin_demo-0.1.0-py3-none-any.whl", now - timedelta(days=3), "b"),
                ("plugin.artifact_deleted", "gdx_plugin_demo-0.2.0-py3-none-any.whl", now - timedelta(days=1), "a"),
                ("plugin.unregistered", "gdx-plugin-other", now.replace(tzinfo=None), "owner"),  # naive → UTC
                ("plugin.unregistered", None, now, "owner"),  # malformed row: ignored
            ]

            class _R:
                def all(self_inner):
                    return rows
            return _R()

    out = rec.recorded_removals(_DB())
    assert set(out) == {"gdx_plugin_demo", "gdx_plugin_other"}
    assert set(out["gdx_plugin_demo"]) == {"0.1.0", "0.2.0"}  # artifact intents are keyed by version
    assert out["gdx_plugin_demo"]["0.2.0"].at > out["gdx_plugin_demo"]["0.1.0"].at
    # two rows for the same (name, version): the LATER one wins, whatever order they come in
    latest = out["gdx_plugin_demo"]["0.1.0"]
    assert latest.at == pytest.approx((now - timedelta(days=2)).timestamp(), abs=2) and latest.by == "a"
    assert set(out["gdx_plugin_other"]) == {None}  # an unregister is version-less
    assert out["gdx_plugin_other"][None].action == "plugin.unregistered"


def test_recorded_removals_unreadable_trail_means_nothing_is_removable():
    class _DB:
        rolled_back = False

        def execute(self, *a, **k):
            raise RuntimeError("no such table: audit_logs")

        def rollback(self):
            self.rolled_back = True

    db = _DB()
    assert rec.recorded_removals(db) == {}
    assert db.rolled_back is True


def test_the_newest_copy_is_what_a_recorded_removal_must_post_date(tmp_path, monkeypatch):
    # prod shape from CLAUDE.md: a stale older dist-info lingering beside the
    # current one. Intent 20 days old, current copy 1 day old → kept; the
    # 90-day-old metadata dir must not make the deletion look newer.
    import os as _os
    now = time.time()
    old_info = _make_dist_info(tmp_path, "demoplug", "0.9")  # stale metadata, no code of its own
    (old_info / "RECORD").write_text(f"{old_info.name}/METADATA,,\n{old_info.name}/RECORD,,\n")
    _os.utime(old_info, (now - 90 * 86400, now - 90 * 86400))
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    _os.utime(info, (now - 86400, now - 86400))
    out = _reconcile_with(monkeypatch, tmp_path, intents={"demoplug": {"1.0": _intent(now - 20 * 86400, "1.0")}})
    assert out.removed == ()
    assert pkg.exists() and info.exists()
    # ...and a removal recorded after the current copy DOES apply, stale dir or not.
    out = _reconcile_with(monkeypatch, tmp_path, intents={"demoplug": {"1.0": _intent(now + 60, "1.0")}})
    assert out.removed == ("demoplug",)
    assert not pkg.exists() and not info.exists()


def test_an_unregistered_filename_shaped_registry_row_still_names_its_plugin():
    # Issue #100's shape: an operator pasted the wheel filename into the package
    # field. Its `plugin.unregistered` intent must key the same distribution the
    # desired-state loop keyed for that row.
    from datetime import UTC, datetime

    class _DB:
        def execute(self, *a, **k):
            class _R:
                def all(self_inner):
                    return [("plugin.unregistered", "gdx_plugin_foo-1.0-py3-none-any.whl", datetime.now(tz=UTC), "o")]
            return _R()

    out = rec.recorded_removals(_DB())
    assert set(out) == {"gdx_plugin_foo"} and set(out["gdx_plugin_foo"]) == {"1.0"}


def test_a_removal_writes_its_audit_row_through_the_real_writer(tmp_path, monkeypatch):
    """Invariant #1 for the act itself: a REAL session on an ORM-built SQLite DB,
    the real intent row from the endpoint's action, the real writer. Removal must
    leave a `plugin.removed_from_volume` row that points back at the intent."""
    from datetime import datetime

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync

    engine = create_engine(f"sqlite:///{tmp_path / 'trail.db'}")
    AuditLog.__table__.create(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('096')"))
    db = sessionmaker(bind=engine, autoflush=False)()
    rec.ensure_registry_table(db)
    rec.ensure_artifact_table(db)

    volume = tmp_path / "plugins"
    volume.mkdir()
    info, pkg = _make_installed_plugin(volume, "goneplug", "1.0")
    import os as _os
    _os.utime(info, (time.time() - 3600, time.time() - 3600))  # installed an hour ago
    log_audit_event_sync(db, tenant_id="t1", user_id="owner", action="plugin.artifact_deleted",
                         entity_type="plugin_artifact", entity_id="goneplug-1.0-py3-none-any.whl", details={})
    db.commit()

    monkeypatch.setattr(rec, "INSTALL_DIR", str(volume))
    out = rec.reconcile(db=db)

    assert out.removed == ("goneplug",)
    assert not pkg.exists() and not info.exists()
    rows = db.execute(text("select entity_id, user_id, details from audit_logs "
                           "where action='plugin.removed_from_volume'")).fetchall()
    assert len(rows) == 1
    entity_id, user_id, details = rows[0]
    assert (entity_id, user_id) == ("goneplug", "plugin-host")
    if isinstance(details, str):
        import json
        details = json.loads(details)
    assert details["intent_action"] == "plugin.artifact_deleted"
    assert details["intent_entity_id"] == "goneplug-1.0-py3-none-any.whl"
    assert details["intent_by"] == "owner"  # who asked for it, copied from the DELETE's row
    assert datetime.fromisoformat(details["intent_recorded_at"]) is not None
    db.close()


def test_a_removal_recorded_for_another_version_does_not_apply_to_this_copy(tmp_path, monkeypatch):
    # prod shape: 0.3.1 and 0.4.0 rows, 0.4.0 on the volume. Tidying the 0.3.1
    # row records an intent for 0.3.1 only. If the 0.4.0 row later vanishes by
    # hand, that old intent must not delete the 0.4.0 copy.
    import os as _os
    now = time.time()
    info, pkg = _make_installed_plugin(tmp_path, "gdx_plugin_chi_pricing", "0.4.0")
    _os.utime(info, (now - 7200, now - 7200))
    out = _reconcile_with(monkeypatch, tmp_path, intents={"gdx_plugin_chi_pricing": {"0.3.1": _intent(now - 3600, "0.3.1")}})
    assert out.removed == ()
    assert pkg.exists() and info.exists()
    # ...whereas a version-less unregister, or a deletion naming THIS version, applies.
    out = _reconcile_with(monkeypatch, tmp_path, intents={"gdx_plugin_chi_pricing": {"0.4.0": _intent(now - 3600, "0.4.0")}})
    assert out.removed == ("gdx_plugin_chi_pricing",)


def test_every_removal_reports_the_intent_it_applied(tmp_path, monkeypatch):
    _make_installed_plugin(tmp_path, "demoplug", "1.0")
    applied: dict[str, rec.RemovalIntent] = {}
    intent = _intent(time.time() + 60, by="doug")
    gone = rec.remove_undesired_plugins(set(), {"demoplug": {None: intent}}, target=str(tmp_path),
                                        applied=applied)
    assert gone == ["demoplug"] and applied == {"demoplug": intent}


def test_an_old_versions_intent_cannot_reach_the_current_code_through_a_lingering_dist_info(tmp_path, monkeypatch):
    # pip --target leaves the old version's dist-info beside the new install
    # (prod had 0.1.0 + 0.1.1 + 0.1.2 side by side). The 0.3.1 row is tidied
    # AFTER 0.4.0 was installed; if the 0.4.0 row later vanishes by hand, that
    # intent names a version the host is not running — the code must stay.
    import os as _os
    now = time.time()
    stale = _make_dist_info(tmp_path, "gdx_plugin_chi_pricing", "0.3.1")  # leftover metadata only
    (stale / "RECORD").write_text(f"{stale.name}/METADATA,,\n{stale.name}/RECORD,,\n")
    (stale / "entry_points.txt").write_text("[gdx.modules]\nchipricing = gdx_plugin_chi_pricing:manifest\n")
    info, pkg = _make_installed_plugin(tmp_path, "gdx_plugin_chi_pricing", "0.4.0")
    _os.utime(info, (now - 7200, now - 7200))
    _os.utime(stale, (now - 86400 * 30, now - 86400 * 30))
    assert rec.effective_version("gdx_plugin_chi_pricing", str(tmp_path)) == "0.4.0"
    out = _reconcile_with(monkeypatch, tmp_path, intents={"gdx_plugin_chi_pricing": {"0.3.1": _intent(now - 3600, "0.3.1")}})
    assert out.removed == ()
    assert pkg.exists() and info.exists()


def test_deleting_the_newest_files_row_while_an_older_one_remains_downgrades_not_removes(tmp_path, monkeypatch):
    """Per-file semantics, pinned as they are (pre-existing, not this change):
    the uploads table lists files; removing the 0.4.0 file's row while the 0.3.1
    row remains leaves the plugin DESIRED at 0.3.1, so the restart installs
    0.3.1 in place of 0.4.0 — a downgrade, not a removal. To remove the plugin
    the operator deletes every one of its rows. The product question (should
    removing the running version's row mean roll back or remove?) is on the
    found-not-filed ledger for a ruling; until then this test says what happens."""
    import os as _os
    now = time.time()
    info, pkg = _make_installed_plugin(tmp_path, "gdx_plugin_chi_pricing", "0.4.0")
    _os.utime(info, (now - 7200, now - 7200))
    calls: list[str] = []
    out = _reconcile_with(monkeypatch, tmp_path,
                          artifacts=[("gdx_plugin_chi_pricing-0.3.1-py3-none-any.whl", "sha", b"")],
                          intents={"gdx_plugin_chi_pricing": {"0.4.0": _intent(now + 60, "0.4.0")}},
                          install_artifact=lambda filename, *a, **k: calls.append(filename) or True)
    assert out.removed == ()  # still desired via the older row
    assert calls == ["gdx_plugin_chi_pricing-0.3.1-py3-none-any.whl"]  # ...so the older file is what gets installed
    assert pkg.exists()


def test_a_spent_old_unregister_does_not_widen_a_fresh_single_file_deletion(tmp_path, monkeypatch):
    info, pkg = _make_installed_plugin(tmp_path, "demoplug", "1.0")
    import os as _os
    now = time.time()
    _os.utime(info, (now - 7200, now - 7200))
    staged = tmp_path / "_artifacts"
    staged.mkdir()
    (staged / "demoplug-0.9-py3-none-any.whl").write_bytes(b"old")
    (staged / "demoplug-1.0-py3-none-any.whl").write_bytes(b"new")
    out = _reconcile_with(monkeypatch, tmp_path, intents={"demoplug": {
        None: _intent(now - 365 * 86400),            # unregistered a year ago: predates this copy, spent
        "1.0": _intent(now + 60, "1.0"),             # this copy's row deleted just now: applies
    }})
    assert out.removed == ("demoplug",)
    assert sorted(p.name for p in staged.iterdir()) == ["demoplug-0.9-py3-none-any.whl"]
