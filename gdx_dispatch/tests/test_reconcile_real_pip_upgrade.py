"""A plugin UPDATE must install the new code, not just the new metadata.

Every other reconcile test monkeypatches `pip_install`, so the one behaviour
that actually bites in production is never exercised: `pip install --target`
does NOT replace an existing package directory. It logs

    WARNING: Target directory .../demoplug already exists. Specify --upgrade
             to force replacement.

skips the code, installs the new dist-info anyway, and **exits 0**. Every
downstream check then believes the metadata — `effective_version` reads
dist-info, so `is_installed` passes and `detect_stale` finds nothing — and
/ready goes green while the process serves the old code. Seen on prod
2026-08-07 upgrading a pricing plugin from 0.1.2 to 0.2.0.

These tests build real wheels and run real pip, so they fail if that hole
reopens. The fixture wheels declare no dependencies, so the real `pip_install`
(which passes neither `--no-index` nor `--no-deps`) resolves nothing and needs
no network.

The other half of the contract matters just as much: plugin-host has NO network
egress, so an upgrade whose deps can't resolve simply fails. Clearing the old
install before knowing that would turn "couldn't upgrade" into "plugin is
gone", every boot. `test_failed_upgrade_keeps_the_working_install` pins that.
"""
from __future__ import annotations

import base64
import hashlib
import zipfile

import pytest

from gdx_dispatch.plugin_host import reconcile as rc

DIST = "demoplug"


def _build_wheel(tmp_path, version: str, marker: str):
    """A minimal but genuine wheel: package code + dist-info with a RECORD."""
    info = f"{DIST}-{version}.dist-info"
    files = {
        f"{DIST}/__init__.py": f'CODE_VERSION = "{marker}"\n',
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {version}\n",
        f"{info}/WHEEL": ("Wheel-Version: 1.0\nGenerator: test\n"
                          "Root-Is-Purelib: true\nTag: py3-none-any\n"),
    }
    records = []
    for name, body in files.items():
        raw = body.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(raw)}")
    records.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = "\n".join(records) + "\n"

    whl = tmp_path / f"{DIST}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return whl


def _code_marker(target) -> str | None:
    init = target / DIST / "__init__.py"
    return init.read_text().strip() if init.exists() else None


@pytest.fixture
def target(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    return d


def _install(tmp_path, target, version, marker) -> bool:
    whl = _build_wheel(tmp_path, version, marker)
    return rc.install_artifact(whl.name, whl.read_bytes(), None, target=str(target))


def test_update_replaces_the_code_not_just_the_metadata(tmp_path, target):
    assert _install(tmp_path, target, "1.0.0", "OLD") is True
    assert _code_marker(target) == 'CODE_VERSION = "OLD"'

    assert _install(tmp_path, target, "2.0.0", "NEW") is True

    # The whole point: the CODE on disk must be the new one. Before the fix
    # this read OLD while effective_version happily reported 2.0.0.
    assert _code_marker(target) == 'CODE_VERSION = "NEW"'
    assert rc.effective_version(DIST, target=str(target)) == "2.0.0"
    assert rc.is_installed(DIST, "2.0.0", target=str(target)) is True
    # And no metadata for the old version is left to confuse version detection.
    assert rc._dist_info_dirs(DIST, target=str(target)) == [f"{DIST}-2.0.0.dist-info"]


def test_failed_upgrade_keeps_the_working_install(tmp_path, target, monkeypatch):
    """A failed upgrade must not destroy the plugin that was working.

    This is the regression that matters most: on the egress-less host, an
    upgrade whose dependencies can't resolve WILL fail. If the old install was
    already cleared by then, the volume is left empty and the plugin vanishes —
    and since the artifact row persists, it vanishes again on every boot. A
    plugin that can't upgrade is a degraded state; a plugin that disappeared is
    an outage.
    """
    assert _install(tmp_path, target, "1.0.0", "OLD") is True

    real_pip = rc.pip_install

    def _pip_fails_only_in_staging(spec, target=rc.INSTALL_DIR):
        if rc._STAGING in str(target):
            return False  # stands in for "dependency cannot resolve offline"
        return real_pip(spec, target=target)

    monkeypatch.setattr(rc, "pip_install", _pip_fails_only_in_staging)

    assert _install(tmp_path, target, "2.0.0", "NEW") is False

    # The working install survived, intact and still reported as installed.
    assert _code_marker(target) == 'CODE_VERSION = "OLD"'
    assert rc.effective_version(DIST, target=str(target)) == "1.0.0"
    assert rc.is_installed(DIST, "1.0.0", target=str(target)) is True


def test_failed_upgrade_leaves_no_staging_litter(tmp_path, target, monkeypatch):
    _install(tmp_path, target, "1.0.0", "OLD")
    monkeypatch.setattr(rc, "pip_install", lambda *a, **k: False)
    _install(tmp_path, target, "2.0.0", "NEW")
    assert not (target / rc._STAGING).exists()


def test_upgrade_drops_a_module_the_new_version_removed(tmp_path, target):
    """Files the old version installed and the new one doesn't must not linger
    as importable stale modules."""
    _install(tmp_path, target, "1.0.0", "OLD")
    orphan = target / DIST / "gone_in_v2.py"
    orphan.write_text("legacy = True\n")
    # Claim it in RECORD so it is recognised as part of the old install.
    record = target / f"{DIST}-1.0.0.dist-info" / "RECORD"
    record.write_text(record.read_text() + f"{DIST}/gone_in_v2.py,,\n")

    assert _install(tmp_path, target, "2.0.0", "NEW") is True
    assert not orphan.exists(), "a module dropped by the new version survived"
    assert _code_marker(target) == 'CODE_VERSION = "NEW"'


def test_reinstalling_the_same_version_is_a_no_op(tmp_path, target):
    assert _install(tmp_path, target, "1.0.0", "OLD") is True
    # A second identical install must not clear and re-pip: on the egress-less
    # host that is the difference between a fast boot and a failed one.
    assert _install(tmp_path, target, "1.0.0", "OLD") is True
    assert _code_marker(target) == 'CODE_VERSION = "OLD"'
    assert rc.effective_version(DIST, target=str(target)) == "1.0.0"


def test_downgrade_also_replaces_the_code(tmp_path, target):
    """Version change is the trigger, not 'newer' — a rollback must land too."""
    assert _install(tmp_path, target, "2.0.0", "NEW") is True
    assert _install(tmp_path, target, "1.0.0", "OLD") is True
    assert _code_marker(target) == 'CODE_VERSION = "OLD"'
    assert rc.effective_version(DIST, target=str(target)) == "1.0.0"


def test_removal_leaves_another_plugins_shared_dependency_alone(tmp_path, target):
    """Cleaning up plugin A must not delete a package plugin B also installed."""
    _install(tmp_path, target, "1.0.0", "OLD")
    # A second distribution whose RECORD also claims the `demoplug` directory,
    # standing in for a shared vendored dependency.
    other = target / "otherplug-1.0.0.dist-info"
    other.mkdir()
    (other / "RECORD").write_text(f"{DIST}/__init__.py,,\notherplug/__init__.py,,\n")
    (target / "otherplug").mkdir()
    (target / "otherplug" / "__init__.py").write_text("x = 1\n")

    removed = rc.remove_installed_dist(DIST, target=str(target))

    assert (target / DIST).exists(), "a path another dist-info claims must survive"
    assert f"{DIST}-1.0.0.dist-info" in removed  # our own metadata still goes
    assert (target / "otherplug" / "__init__.py").exists()


def test_removal_refuses_paths_outside_the_install_dir(tmp_path, target):
    """A crafted RECORD must not point our own cleanup at someone else's files."""
    outside = tmp_path / "precious.txt"
    outside.write_text("do not delete me")
    info = target / f"{DIST}-9.9.9.dist-info"
    info.mkdir()
    (info / "RECORD").write_text(
        f"../precious.txt,,\n/etc/passwd,,\n{DIST}/__init__.py,,\n"
    )

    rc.remove_installed_dist(DIST, target=str(target))

    assert outside.exists(), "cleanup escaped the install dir"


def test_a_broken_swap_does_not_look_installed(tmp_path, target, monkeypatch):
    """If the swap dies partway, the volume must NOT read as "new version OK".

    Metadata is moved last precisely so this can't happen: new dist-info over
    missing code would make effective_version report the new version, is_installed
    return True, the next boot skip the install, and /ready go green over a plugin
    that cannot import. Absent metadata instead means the next boot reinstalls.
    """
    _install(tmp_path, target, "1.0.0", "OLD")

    real_move = rc.shutil.move
    state = {"n": 0}

    def _move_dies_after_first(src, dst):
        state["n"] += 1
        if state["n"] > 1:
            raise OSError("disk full mid-swap")
        return real_move(src, dst)

    monkeypatch.setattr(rc.shutil, "move", _move_dies_after_first)
    with pytest.raises(OSError):
        _install(tmp_path, target, "2.0.0", "NEW")
    monkeypatch.undo()

    # Whatever landed, it must not claim to be a complete 2.0.0 install.
    assert rc.is_installed(DIST, "2.0.0", target=str(target)) is False


def test_record_with_a_nul_byte_does_not_abort_the_boot(tmp_path, target):
    """A crafted RECORD must be refused, not raise through reconcile.

    os.path.realpath raises ValueError (not OSError) on an embedded NUL, which
    would escape mid-removal and skip every remaining plugin that boot.
    """
    _install(tmp_path, target, "1.0.0", "OLD")
    record = target / f"{DIST}-1.0.0.dist-info" / "RECORD"
    record.write_text("bad\x00name/file.py,,\n" + record.read_text())

    removed = rc.remove_installed_dist(DIST, target=str(target))  # must not raise

    assert (tmp_path / "plugins").exists()
    assert f"{DIST}-1.0.0.dist-info" not in [p.name for p in target.iterdir()]
    assert isinstance(removed, list)


def test_upgrade_does_not_clobber_another_plugins_shared_package(tmp_path, target):
    """Upgrading plugin A must not overwrite a package plugin B also installed."""
    _install(tmp_path, target, "1.0.0", "OLD")
    # Plugin B claims the same top-level name (a shared vendored dependency).
    other = target / "otherplug-1.0.0.dist-info"
    other.mkdir()
    (other / "RECORD").write_text(f"{DIST}/__init__.py,,\n")
    (target / DIST / "shared_marker.py").write_text("owned_by = 'B'\n")

    assert _install(tmp_path, target, "2.0.0", "NEW") is True

    assert (target / DIST / "shared_marker.py").exists(), (
        "plugin B's file was destroyed by plugin A's upgrade"
    )


def test_reconcile_installs_only_the_newest_row_per_plugin(monkeypatch):
    """Uploads never delete older rows, so a plugin updated twice has three.

    Installing each in turn would clear and re-pip the same distribution once
    per row on every boot, each cycle another chance to fail partway. Only the
    newest is installed — and "newest" is by version, so 0.10.0 beats 0.9.0.
    """
    rows = [
        (f"{DIST}-0.9.0-py3-none-any.whl", "sha-a", b"a"),
        (f"{DIST}-0.10.0-py3-none-any.whl", "sha-b", b"b"),
        (f"{DIST}-0.1.0-py3-none-any.whl", "sha-c", b"c"),
        ("otherplug-1.0.0-py3-none-any.whl", "sha-d", b"d"),
    ]
    # Hand-written expected order — NOT built with _artifact_sort_key, so this
    # catches a sort regression instead of restating the function under test.
    ordered = [rows[2], rows[0], rows[1], rows[3]]  # 0.1.0, 0.9.0, 0.10.0, other
    assert [r[0] for r in sorted(rows, key=lambda r: rc._artifact_sort_key(r[0]))] == \
        [r[0] for r in ordered], "artifact ordering is not oldest-first by version"
    monkeypatch.setattr(rc, "desired_artifacts", lambda db: ordered)
    monkeypatch.setattr(rc, "desired_packages", lambda db: [])
    monkeypatch.setattr(rc, "ensure_registry_table", lambda db: None)
    monkeypatch.setattr(rc, "ensure_artifact_table", lambda db: None)

    seen = []
    monkeypatch.setattr(rc, "install_artifact",
                        lambda fn, content, expected_sha256=None: seen.append(fn) or True)

    result = rc.reconcile(db=object())

    assert seen == [f"{DIST}-0.10.0-py3-none-any.whl", "otherplug-1.0.0-py3-none-any.whl"]
    assert set(result.installed) == set(seen)
    assert result.failed == []


def test_artifacts_sort_by_version_not_filename():
    """0.10.0 must be treated as newer than 0.9.0 — lexicographic order is a
    silent downgrade once a plugin reaches a two-digit minor."""
    names = [f"{DIST}-0.9.0-py3-none-any.whl", f"{DIST}-0.10.0-py3-none-any.whl"]
    ordered = sorted(names, key=rc._artifact_sort_key)
    # Last one wins the install, so the newest version must sort last.
    assert ordered[-1] == f"{DIST}-0.10.0-py3-none-any.whl"
