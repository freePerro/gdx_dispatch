"""Reconcile installed plugins to the plugin_registry (ADR-013 step 5).

The plugin_registry table is the operator's desired-state list (written via the
owner-only admin endpoint). At plugin-host boot, reconcile() pip-installs each
registered package into the /plugins volume (which persists across restarts) and
puts it on sys.path so discovery finds it. This is how in-app install works
without running pip inside the core app: the operator records intent, plugin-host
materializes it on restart.

Pure helpers (ensure_registry_table / desired_packages / pip_install) are
separated so they unit-test with a fake DB / mocked subprocess.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)

INSTALL_DIR = os.getenv("PLUGIN_INSTALL_DIR", "/plugins")

# Hard bound on a single pip invocation. plugin-host has NO network egress in
# production, so a spec whose deps aren't already vendored will never resolve —
# without this it hangs uvicorn's import of main:app for minutes, taking the
# whole plugin surface down (the 2026-06-29 deploy outage). Fail fast instead.
PIP_TIMEOUT_S = int(os.getenv("PLUGIN_PIP_TIMEOUT", "60"))


class ReconcileResult(NamedTuple):
    """Outcome of a reconcile pass. `installed` are specs newly pip-installed
    this boot; `failed` are desired specs that neither were already present nor
    installed cleanly — the caller surfaces these so a half-loaded host is loud,
    not silent."""
    installed: list[str]
    failed: list[str]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS plugin_registry (
    id        SERIAL PRIMARY KEY,
    package   TEXT NOT NULL UNIQUE,
    version   TEXT,
    added_at  TIMESTAMPTZ DEFAULT now(),
    added_by  TEXT
)
"""

# Uploaded plugin artifacts (private/local plugins that aren't on a pip index).
# Stored in the DB so core (which receives the upload) and plugin-host (which
# installs it) share state without a shared volume — same pattern as the registry.
_ARTIFACT_SQL = """
CREATE TABLE IF NOT EXISTS plugin_artifact (
    id          SERIAL PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    sha256      TEXT NOT NULL,
    content     BYTEA NOT NULL,
    uploaded_at TIMESTAMPTZ DEFAULT now(),
    uploaded_by TEXT
)
"""

# A wheel/sdist basename: word chars, dot, dash, plus; must end .whl/.tar.gz.
# No path separators -> blocks traversal when we write it to disk.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]+\.(whl|tar\.gz)$")


def safe_artifact_name(filename: str) -> str | None:
    """Return the validated basename, or None if it's unsafe / wrong type.
    Strips any directory part first so an upload can't traverse paths."""
    base = os.path.basename((filename or "").strip())
    return base if _SAFE_NAME.match(base) else None


def _canon(name: str) -> str:
    """PEP 503-ish canonical form: runs of -_. collapse to one _, lowercased.
    `gdx-plugin-chi-pricing` and `gdx_plugin_chi_pricing` both -> the same key,
    so a registry package name and a wheel's distribution name compare equal."""
    return re.sub(r"[-_.]+", "_", (name or "")).lower()


def _versions_equal(a: str, b: str) -> bool:
    """PEP 440-aware version equality. pip writes the *normalized* version into
    dist-info (`1.0-1` -> `1.0.post1`, `v1.2` -> `1.2`), so a raw string compare
    against a registry/filename version would spuriously miss and reinstall every
    boot. Fall back to a literal compare only if either side won't parse."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging ships with pip
        return a == b
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def artifact_name_version(filename: str) -> tuple[str | None, str | None]:
    """(distribution, version) parsed from a wheel/sdist basename, else
    (None, None). Wheel grammar is `{dist}-{version}(-{build})?-{py}-{abi}-{plat}.whl`
    and sdist is `{dist}-{version}.tar.gz`; in both the first two dash-fields are
    distribution and version."""
    base = os.path.basename((filename or "").strip())
    low = base.lower()
    for ext in (".whl", ".tar.gz"):
        if low.endswith(ext):
            parts = base[: len(base) - len(ext)].split("-")
            # Need both a distribution and a non-empty version (`foo-.whl` parses
            # to ('foo', '') which is useless — treat as unparseable).
            if len(parts) >= 2 and parts[0] and parts[1]:
                return parts[0], parts[1]
            return None, None
    return None, None


_ARTIFACT_EXTS = (".whl", ".tar.gz", ".tgz", ".zip", ".egg")


def looks_like_artifact_filename(name: str | None) -> bool:
    """True if `name` is a package FILE, not an index package name — i.e. an
    operator pasted a wheel/sdist filename into the free-text package field
    (issue #100). A PyPI package name can never contain a path separator nor end
    in one of these archive extensions. Case- and whitespace-insensitive so
    ``Plugin.WHL`` / ``x.whl `` don't slip through to a bare-filename pip install.
    Detection is deliberately separate from :func:`artifact_name_version` parsing:
    a filename we recognize but can't parse (``foo-.whl``) must still be refused,
    not fed to pip."""
    s = (name or "").strip().lower()
    return "/" in s or "\\" in s or s.endswith(_ARTIFACT_EXTS)


def installed_versions(distribution: str | None, target: str = INSTALL_DIR) -> set[str]:
    """ALL versions of `distribution` with a dist-info in the target volume.

    Returns a set, not one value, because `pip install --target` does NOT remove
    a prior version's dist-info — the volume accumulates them (prod had chi-pricing
    0.1.0 + 0.1.1 + 0.1.2 dist-info side by side after two upgrades). Reading "the
    first one" silently picked the OLDEST and made a current install look stale
    (2026-06-29 follow-up). Read via importlib.metadata so names/versions match
    exactly how pip wrote them."""
    if not distribution:
        return set()
    from importlib.metadata import distributions

    want = _canon(distribution)
    out: set[str] = set()
    try:
        for dist in distributions(path=[target]):
            if _canon(dist.metadata["Name"]) == want:
                out.add(dist.version)
    except Exception:  # unreadable target / metadata — treat as not installed
        return set()
    return out


def effective_version(distribution: str | None, target: str = INSTALL_DIR) -> str | None:
    """The version whose CODE is actually importable from the volume, or None.

    `pip install --target` overwrites the single package dir in place but leaves
    each version's dist-info behind, so when several accumulate, the LAST install
    is the one whose code is on disk. Installs are monotonic upgrades, so we take
    the highest version (PEP 440 order) as the running one — strictly better than
    reading "the first dist-info" (which picked the OLDEST and caused the v1.5.1
    false-stale). ASSUMES dist-info reflects code: a partial install that wrote
    newer metadata over older code would read high — `prune_other_versions` keeps
    the volume single-version so this stays unambiguous in steady state."""
    vers = installed_versions(distribution, target)
    if not vers:
        return None
    try:
        from packaging.version import Version
        return max(vers, key=Version)
    except Exception:  # pragma: no cover - packaging ships with pip
        return max(vers)


def is_installed(distribution: str | None, version: str | None,
                 target: str = INSTALL_DIR) -> bool:
    """True if the running (effective) version of `distribution` equals `version`.
    This is what makes reconcile idempotent: the /plugins volume persists across
    restarts, so a plugin already installed at the desired version must NOT be
    reinstalled — reinstalling re-resolves its deps against PyPI, which the
    network-isolated host can't reach (the 2026-06-29 outage). Comparing the
    EFFECTIVE (highest) version, not mere dist-info membership, so accumulated old
    metadata can't make a present version look absent NOR a stale one look fresh."""
    if not distribution or not version:
        return False
    eff = effective_version(distribution, target)
    return eff is not None and _versions_equal(eff, version)


def prune_other_versions(distribution: str | None, keep_version: str | None,
                         target: str = INSTALL_DIR) -> list[str]:
    """Delete dist-info dirs for `distribution` whose version != keep_version, and
    return the names removed. This is the root-cause fix for the cruft pip --target
    leaves behind: without it the volume keeps every past version's metadata, which
    makes version detection ambiguous. Call ONLY once keep_version is confirmed
    present, so a working install's metadata is never deleted out from under it."""
    removed: list[str] = []
    if not distribution or not keep_version:
        return removed
    want = _canon(distribution)
    try:
        entries = os.listdir(target)
    except FileNotFoundError:
        return removed
    for entry in entries:
        if not entry.endswith(".dist-info"):
            continue
        name, ver = artifact_name_version(entry[: -len(".dist-info")] + ".whl")
        if name and _canon(name) == want and ver and not _versions_equal(ver, keep_version):
            shutil.rmtree(os.path.join(target, entry), ignore_errors=True)
            removed.append(entry)
            log.info("pruned stale dist-info %s (keeping %s %s)", entry, distribution, keep_version)
    return removed


#: Where a version-changing install is built before it replaces the live one.
_STAGING = "_staging"


def _within(target_real: str, path: str) -> bool:
    """True if `path` really resolves inside `target_real` (symlinks included).

    Never raises: a crafted RECORD entry (an embedded NUL makes realpath raise
    ValueError, not OSError) must be refused, not allowed to abort a reconcile
    halfway through and take the rest of the boot's plugins with it.
    """
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    return real == target_real or real.startswith(target_real + os.sep)


def _shared_top_levels(exclude: list[str], target: str = INSTALL_DIR) -> set[str]:
    """Top-level names claimed by installed dist-infos OTHER than `exclude`."""
    shared: set[str] = set()
    try:
        entries = os.listdir(target)
    except OSError:
        return shared
    for entry in entries:
        if entry.endswith(".dist-info") and entry not in exclude:
            shared |= _record_top_levels(entry, target)
    return shared


def _dist_info_dirs(distribution: str | None, target: str = INSTALL_DIR) -> list[str]:
    """dist-info directory names in `target` belonging to `distribution`."""
    if not distribution:
        return []
    want = _canon(distribution)
    try:
        entries = os.listdir(target)
    except OSError:
        return []
    out = []
    for entry in entries:
        if not entry.endswith(".dist-info"):
            continue
        name, _ = artifact_name_version(entry[: -len(".dist-info")] + ".whl")
        if name and _canon(name) == want:
            out.append(entry)
    return out


def _record_top_levels(dist_info: str, target: str = INSTALL_DIR) -> set[str]:
    """The top-level names a dist-info's RECORD claims it installed.

    RECORD lists every installed file relative to the target dir, so the first
    path component of each line is a top-level file or package directory.
    Entries that are absolute or try to escape the target are ignored — a
    malicious wheel must not be able to point our own cleanup at /etc.
    """
    names: set[str] = set()
    record = os.path.join(target, dist_info, "RECORD")
    try:
        with open(record, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return names
    for line in lines:
        path = line.split(",", 1)[0].strip()
        if not path or "\x00" in path or os.path.isabs(path) or path.startswith(("/", "\\")):
            continue
        head = path.replace("\\", "/").split("/", 1)[0]
        if not head or head in (".", "..", "_artifacts", _STAGING):
            continue
        names.add(head)
    return names


def remove_installed_dist(distribution: str | None, target: str = INSTALL_DIR) -> list[str]:
    """Delete a distribution's installed CODE (and metadata) from the volume.

    This exists because `pip install --target` does NOT replace an existing
    package directory: it logs "Target directory ... already exists. Specify
    --upgrade to force replacement.", **skips the code**, installs the new
    dist-info anyway, and exits 0. The volume is then a lie — metadata says the
    new version, the importable code is the old one — and every downstream
    check believes the metadata (`effective_version` reads dist-info, so
    `is_installed` and `detect_stale` both pass, and /ready goes green over
    stale code). Proven on prod 2026-08-07 upgrading a pricing plugin, and
    reproduced with real pip while writing this. `--upgrade` is not the fix: it
    forces an index check the egress-less host cannot satisfy.

    Only removes top-level entries this distribution owns EXCLUSIVELY — a name
    another installed dist-info also claims (a shared dependency) is left
    alone, so cleaning up plugin A cannot break plugin B.
    """
    removed: list[str] = []
    mine = _dist_info_dirs(distribution, target)
    if not mine:
        return removed

    owned: set[str] = set()
    for d in mine:
        owned |= _record_top_levels(d, target)
    shared: set[str] = set()
    for entry in os.listdir(target):
        if entry.endswith(".dist-info") and entry not in mine:
            shared |= _record_top_levels(entry, target)

    target_real = os.path.realpath(target)
    for name in sorted((owned - shared) | set(mine)):
        if name in ("_artifacts", _STAGING):
            continue
        path = os.path.join(target, name)
        # Containment check: never follow a symlink or a crafted name out of
        # the install dir.
        if not _within(target_real, path):
            log.error("refusing to remove %r — outside the install dir", name)
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path) or os.path.islink(path):
            try:
                os.remove(path)
            except OSError:
                continue
        else:
            continue
        # Only claim what actually went: rmtree(ignore_errors=True) can leave
        # the directory behind, and a removal list that overstates itself is a
        # small lie in exactly the place we are trying to stop lying.
        if not os.path.exists(path) and not os.path.islink(path):
            removed.append(name)
    if removed:
        log.info("removed installed %s from the volume: %s", distribution, ", ".join(removed))
    skipped = owned & shared
    if skipped:
        log.info("kept shared paths another plugin also installs: %s", ", ".join(sorted(skipped)))
    return removed


def ensure_registry_table(db: Session) -> None:
    """Idempotently create plugin_registry. Kept as raw DDL (no Alembic) because
    it's a tiny aux table both core and plugin-host touch; a migration would just
    add coordination overhead."""
    db.execute(text(_CREATE_SQL))
    db.commit()


def desired_packages(db: Session) -> list[tuple[str, str | None]]:
    rows = db.execute(
        text("SELECT package, version FROM plugin_registry ORDER BY package")
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def ensure_artifact_table(db: Session) -> None:
    db.execute(text(_ARTIFACT_SQL))
    db.commit()


def _artifact_sort_key(filename: str) -> tuple:
    """Sort artifacts by (distribution, PEP 440 version) — never by filename.

    `ORDER BY filename` is lexicographic, so `…-0.10.0-…whl` sorts BEFORE
    `…-0.9.0-…whl`. Since the last install of a distribution is the one whose
    code ends up on the volume (and `desired_versions` is likewise last-wins),
    a plugin that reached a two-digit minor would install 0.10.0 and then
    silently reinstall 0.9.0 over it — a self-consistent downgrade, with
    stale-detection agreeing everything was fine.
    """
    dist, ver = artifact_name_version(filename)
    key = _canon(dist or filename)
    try:
        from packaging.version import Version
        return (key, 0, Version(ver or "0"), filename)
    except Exception:
        # Unparseable version sorts first so a real version always wins the
        # last-write, rather than an unorderable name deciding it.
        return (key, -1, ver or "", filename)


def desired_artifacts(db: Session) -> list[tuple[str, str, bytes]]:
    """(filename, sha256, content) for every uploaded plugin artifact, oldest
    version first per distribution so the newest is the one that lands."""
    rows = db.execute(
        text("SELECT filename, sha256, content FROM plugin_artifact")
    ).fetchall()
    rows = sorted(rows, key=lambda r: _artifact_sort_key(r[0]))
    return [(r[0], r[1], bytes(r[2])) for r in rows]


def desired_artifact_names(db: Session) -> list[str]:
    """Just the filenames (no blobs) — for cheap desired-version lookups."""
    rows = db.execute(text("SELECT filename FROM plugin_artifact")).fetchall()
    return [r[0] for r in sorted(rows, key=lambda r: _artifact_sort_key(r[0]))]


def desired_versions(db: Session) -> dict[str, str]:
    """{canonical distribution name: desired version} across registry packages
    and uploaded artifacts — the operator's intended version per plugin dist.
    Used to detect a STALE loaded plugin (installed version != desired)."""
    out: dict[str, str] = {}
    for package, version in desired_packages(db):
        if version:
            out[_canon(package)] = version
    for filename in desired_artifact_names(db):
        dist, ver = artifact_name_version(filename)
        if dist and ver:
            out[_canon(dist)] = ver
    return out


def detect_stale(
    desired: dict[str, str],
    discovered: list[tuple[Any, str | None, str | None]],
    target: str = INSTALL_DIR,
) -> dict[str, dict[str, str]]:
    """Which loaded plugins are at the WRONG version. `discovered` is
    [(manifest, dist_name, dist_version)]. Returns {plugin_key: {installed,
    desired}} for any plugin whose EFFECTIVE installed version != the operator's
    desired version — these get their LIVE endpoints withheld (fail closed) so a
    stale build can't serve over the proxy (2026-06-29 follow-up).

    Compares the effective (highest-on-disk) version, not the single dist_version
    off the entry point (ambiguous when dist-info accumulates) and not mere
    membership (which would let a stale version masquerade as fresh). Best-effort:
    a plugin whose entry point has no resolvable distribution or no desired version
    recorded is NOT flagged — detection needs both."""
    stale: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for manifest, dist_name, _dist_ver in discovered:
        if manifest.key in seen:
            continue
        seen.add(manifest.key)
        want = desired.get(_canon(dist_name)) if dist_name else None
        if not want:
            continue
        if not is_installed(dist_name, want, target):
            stale[manifest.key] = {
                "installed": effective_version(dist_name, target) or "unknown",
                "desired": want,
            }
    return stale


def install_artifact(
    filename: str, content: bytes, expected_sha256: str | None = None,
    target: str = INSTALL_DIR,
) -> bool:
    """Write an uploaded wheel/sdist to a staging path under the volume and
    pip-install it. Filename is re-validated here (defense in depth) so a bad row
    can't path-traverse on write; and if a digest is supplied the bytes are
    verified against it (catches a tampered/corrupted DB row) before install."""
    safe = safe_artifact_name(filename)
    if safe is None:
        log.error("refusing unsafe artifact filename: %r", filename)
        return False
    dist, ver = artifact_name_version(safe)
    if is_installed(dist, ver, target):
        log.info("artifact %s already installed (%s %s) — skipping reinstall", safe, dist, ver)
        # Confirmed present → it's safe to clean any older dist-info cruft left by
        # past --target upgrades (the duplicate-dist-info bug, 2026-06-29 v1.5.1).
        prune_other_versions(dist, ver, target)
        return True
    if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
        log.error("artifact %s sha256 mismatch — refusing to install", safe)
        return False
    staged_dir = os.path.join(target, "_artifacts")
    os.makedirs(staged_dir, exist_ok=True)
    path = os.path.join(staged_dir, safe)
    with open(path, "wb") as fh:
        fh.write(content)

    # A DIFFERENT version of this distribution is already on the volume. pip
    # --target will not overwrite its package directory — it warns, skips the
    # code, still writes the new dist-info, and exits 0 — so installing straight
    # in leaves stale code under fresh metadata. Build the replacement in a
    # staging dir first and swap it in only once pip has actually succeeded.
    present = effective_version(dist, target)
    if dist and present and ver and not _versions_equal(present, ver):
        return _install_replacing(path, dist, ver, target, present)

    ok = pip_install(path, target=target)
    if ok:
        # Only prune AFTER a successful install — never delete a working version's
        # metadata because a new install failed (offline host).
        prune_other_versions(dist, ver, target)
    return ok


def _install_replacing(spec: str, dist: str, ver: str, target: str,
                       present: str) -> bool:
    """Swap an installed distribution to a different version, atomically enough.

    The destructive part of a replace — clearing the old code — happens ONLY
    after pip has produced a complete new install in a staging directory. That
    ordering is the whole point: plugin-host has no network egress, so a wheel
    whose dependencies aren't vendored simply cannot install here. Clearing
    first and then discovering that would turn a plugin that merely *couldn't
    upgrade* into one that no longer exists — a degraded state promoted to an
    outage, repeated on every boot because the artifact row persists.

    On failure the live install is untouched and we return False, which
    reconcile() reports as a failed spec (loud, /ready 503) exactly as before.
    """
    staging = os.path.join(target, _STAGING)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        if not pip_install(spec, target=staging):
            log.error(
                "install of %s %s failed — keeping the working %s install in place",
                dist, ver, present,
            )
            return False

        produced = sorted(os.listdir(staging))
        if not produced:
            log.error("staged install of %s %s produced nothing — keeping %s",
                      dist, ver, present)
            return False

        log.info("replacing %s %s with %s", dist, present, ver)
        # What another installed plugin also claims — computed BEFORE we remove
        # anything, and left alone below. pip's skip-existing behaviour used to
        # protect shared dependency directories; clobbering them here would
        # break plugin B as a side effect of upgrading plugin A.
        shared = _shared_top_levels(_dist_info_dirs(dist, target), target)
        # Safe now: the new install is complete and on disk. This clears files
        # the old version had that the new one dropped, which a plain overwrite
        # would leave behind as importable stale modules.
        remove_installed_dist(dist, target)

        # Code first, metadata LAST. If a move fails partway, the volume must
        # not be left holding the new dist-info over missing code — that reads
        # as "correct version installed" to effective_version/is_installed, so
        # the next boot would skip the install and serve a plugin that cannot
        # import, with /ready green. Without the new dist-info the version reads
        # absent and the next boot reinstalls: a broken swap self-heals.
        code_first = sorted(produced, key=lambda n: (n.endswith(".dist-info"), n))
        target_real = os.path.realpath(target)
        for name in code_first:
            dest = os.path.join(target, name)
            if not _within(target_real, dest):
                log.error("refusing to install %r — outside the install dir", name)
                continue
            if name in shared and (os.path.exists(dest) or os.path.islink(dest)):
                log.info("keeping existing %r — another installed plugin claims it", name)
                continue
            if os.path.isdir(dest) and not os.path.islink(dest):
                shutil.rmtree(dest, ignore_errors=True)
            elif os.path.exists(dest) or os.path.islink(dest):
                os.remove(dest)
            shutil.move(os.path.join(staging, name), dest)
        prune_other_versions(dist, ver, target)
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def pip_install(spec: str, target: str = INSTALL_DIR) -> bool:
    """Install one spec into the target dir. Returns True on success, logs on
    failure (never raises — one bad package must not abort the whole boot).

    Boot-safety against the network-isolated host comes from is_installed()
    skipping the already-present steady state entirely; this function only runs
    when an install is genuinely needed. `--retries 0` + `--timeout 10` + a
    wall-clock subprocess timeout ensure that when the index is unreachable (a
    new/changed plugin whose deps aren't vendored) pip FAILS in seconds rather
    than hanging boot for minutes (the 2026-06-29 outage). `--upgrade` is
    deliberately omitted: it forces an index check, and an offline host can't
    satisfy it anyway — a version change needs network at install time."""
    cmd = [sys.executable, "-m", "pip", "install", "--target", target,
           "--retries", "0", "--timeout", "10", spec]
    log.info("plugin reconcile: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        log.error(
            "pip install timed out after %ss for %s — plugin-host has no network "
            "egress, so any dependency not already vendored in %s cannot resolve",
            PIP_TIMEOUT_S, spec, target,
        )
        return False
    if result.returncode != 0:
        log.error("pip install failed for %s: %s", spec, (result.stderr or "")[-500:])
        return False
    return True


def reconcile(db: Session | None = None) -> ReconcileResult:
    """Bring the volume in line with desired-state (registry packages + uploaded
    artifacts) and report what installed vs. what failed. Already-present versions
    are skipped (the volume persists across restarts), so the steady state needs
    no network. Adds the install dir to sys.path so freshly-installed plugins are
    importable in this process."""
    own = db is None
    db = db or SessionLocal()
    installed: list[str] = []
    failed: list[str] = []
    try:
        ensure_registry_table(db)
        ensure_artifact_table(db)
        for package, version in desired_packages(db):
            # An operator can paste a wheel/sdist *filename* into the free-text
            # package field of the install UI (issue #100). A filename is not a pip
            # spec: `pip install <bare filename>` resolves it against CWD (/app),
            # never finds it, and fails EVERY boot — wedging /ready 503 forever even
            # though the plugin is already installed from its uploaded artifact.
            # NEVER feed a filename to pip: when the volume already has that dist
            # (the common case — it's installed via the artifact loop below), skip
            # and prune; otherwise skip with a loud warning (the uploaded artifact,
            # if any, is the real installer — a filename here is operator cruft, not
            # a genuine degraded plugin, so it must NOT gate /ready). A real package
            # name is not filename-shaped and falls through unchanged. NOTE: this
            # neutralizes the filename INSTANCE; a typo'd *package name* that can't
            # install offline still (correctly) surfaces as a failed spec.
            if looks_like_artifact_filename(package):
                fdist, fver = artifact_name_version(package)
                if fdist and fver and is_installed(fdist, fver):
                    log.info("registry row %r resolves to installed %s %s — skipping",
                             package, fdist, fver)
                    prune_other_versions(fdist, fver, target=INSTALL_DIR)
                else:
                    log.warning(
                        "registry row %r is a plugin filename, not a package spec — "
                        "skipping (upload the file via the artifact installer; this row "
                        "is ignored, not installed)", package)
                continue
            spec = f"{package}=={version}" if version else package
            if version and is_installed(package, version):
                log.info("registry package %s already installed — skipping", spec)
                prune_other_versions(package, version, target=INSTALL_DIR)
                continue
            # Same stale-code trap as the artifact path: pip --target won't
            # overwrite an existing package dir, so a version bump on an
            # already-installed distribution has to go through the staged
            # replace rather than a bare install.
            present = effective_version(package, INSTALL_DIR) if version else None
            if version and present and not _versions_equal(present, version):
                ok = _install_replacing(spec, package, version, INSTALL_DIR, present)
            else:
                ok = pip_install(spec)
                if ok and version:
                    prune_other_versions(package, version, target=INSTALL_DIR)
            if ok:
                installed.append(spec)
            else:
                failed.append(spec)
        # Uploaded private plugins (not on any index). Verify the stored digest
        # before installing — a corrupted/tampered row won't be executed.
        # Only the newest artifact per distribution is installed. Uploads never
        # delete older rows, so a plugin that has been updated a few times has
        # several; installing each in turn would clear and re-pip the same
        # distribution once per row on every boot — wasted work, and a fresh
        # chance for a mid-sequence failure each time. desired_artifacts is
        # sorted oldest-first per distribution, so the last row for a
        # distribution is the one to install.
        artifacts = desired_artifacts(db)
        newest = {}
        for filename, sha256, content in artifacts:
            adist, _ = artifact_name_version(filename)
            newest[_canon(adist or filename)] = (filename, sha256, content)
        superseded = len(artifacts) - len(newest)
        if superseded:
            log.info("skipping %d superseded artifact row(s) — installing the "
                     "newest version of each plugin only", superseded)
        for filename, sha256, content in newest.values():
            if install_artifact(filename, content, expected_sha256=sha256):
                installed.append(filename)
            else:
                failed.append(filename)
    finally:
        if own:
            db.close()
    if INSTALL_DIR not in sys.path:
        sys.path.insert(0, INSTALL_DIR)
    if failed:
        log.error("plugin reconcile finished with %d failed spec(s): %s",
                  len(failed), ", ".join(failed))
    return ReconcileResult(installed=installed, failed=failed)
