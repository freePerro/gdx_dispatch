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

import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import select, text
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
    not silent. `removed` are plugin distributions deleted from the volume
    because nothing desired them any more (see remove_undesired_plugins)."""
    installed: list[str]
    failed: list[str]
    removed: tuple[str, ...] = ()

# Raw DDL, not Alembic (tiny aux tables both core and plugin-host touch). But
# the repo rule that schema must run on BOTH SQLite and Postgres still applies.
#
# Measured, not assumed — of the Postgres-isms here, sqlite3 accepts `SERIAL`,
# `BYTEA` and `TIMESTAMPTZ` as type names (it takes arbitrary ones and assigns
# affinity). The ONLY token that actually raised was `DEFAULT now()`:
#     sqlite3.OperationalError: near "(": syntax error
# `SERIAL` is still mapped, because SQLite gives it NUMERIC affinity and the
# column would not autoincrement — parsing is not the same as working.
#
# It mattered because `ensure_registry_table` runs on every call into the plugin
# admin API, so that one token made the whole surface unusable on a SQLite dev
# instance and untestable in the default (SQLite) suite.
_PG_TYPES = {"serial": "SERIAL PRIMARY KEY", "ts": "TIMESTAMPTZ DEFAULT now()", "blob": "BYTEA"}
_SQLITE_TYPES = {
    "serial": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "ts": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "blob": "BLOB",
}


def _types_for(db: Session) -> dict[str, str]:
    """Pick the dialect's types. FAILS CLOSED to SQLite, deliberately.

    An earlier version walked `getattr(db, "bind", ...)`, which returns None on
    a Session bound via its sessionmaker rather than directly — so it silently
    chose the POSTGRES branch on SQLite and reintroduced the exact bug. Use
    `get_bind()`, which resolves the real engine; and if the dialect genuinely
    cannot be determined, prefer the SQLite forms: they execute on Postgres
    (`INTEGER PRIMARY KEY AUTOINCREMENT` is the only lossy one) whereas the
    Postgres forms raise on SQLite. A guard must not fail open into the defect
    it exists to prevent.
    """
    try:
        name = db.get_bind().dialect.name
    except Exception:  # no bind resolvable — take the portable branch
        return _SQLITE_TYPES
    return _PG_TYPES if name == "postgresql" else _SQLITE_TYPES


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS plugin_registry (
    id        {serial},
    package   TEXT NOT NULL UNIQUE,
    version   TEXT,
    added_at  {ts},
    added_by  TEXT
)
"""

# Uploaded plugin artifacts (private/local plugins that aren't on a pip index).
# Stored in the DB so core (which receives the upload) and plugin-host (which
# installs it) share state without a shared volume — same pattern as the registry.
_ARTIFACT_SQL = """
CREATE TABLE IF NOT EXISTS plugin_artifact (
    id          {serial},
    filename    TEXT NOT NULL UNIQUE,
    sha256      TEXT NOT NULL,
    content     {blob} NOT NULL,
    uploaded_at {ts},
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


def running_dists(discovered, target: str = INSTALL_DIR) -> dict:
    """{plugin key: (distribution, running version)} for the catalog.

    Deliberately NOT the entry point's own `dist.version`. `pip install --target`
    leaves every past version's dist-info behind, so the dist an entry point
    resolves from can be any of them — prod once had three side by side, and
    reading the wrong one is what caused the v1.5.1 false-stale. `detect_stale`
    refuses that value for the same reason and compares `effective_version`;
    publishing a different number than the one the staleness check trusts would
    put a contradiction on the admin screen. Falls back to the entry-point
    version only when nothing is installed under `target` (a plugin on the
    image's own path rather than the volume).
    """
    out: dict = {}
    for manifest, dist_name, ep_version in discovered:
        out[manifest.key] = (dist_name, effective_version(dist_name, target) or ep_version)
    return out


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


_PLUGIN_ENTRY_POINT_GROUP = "[gdx.modules]"


def plugin_dists_on_volume(target: str = INSTALL_DIR) -> dict[str, str]:
    """{canonical distribution: distribution name} for every distribution
    installed under `target` that declares a `gdx.modules` entry point — a
    plugin, in other words. Read from each dist-info's entry_points.txt, never
    by importing anything. Libraries that also live in the volume (playwright,
    greenlet, pyee, ...) declare no such group and never appear here, so a
    cleanup keyed on this map cannot touch them."""
    out: dict[str, str] = {}
    try:
        entries = os.listdir(target)
    except OSError:
        return out
    for entry in entries:
        if not entry.endswith(".dist-info"):
            continue
        ep = os.path.join(target, entry, "entry_points.txt")
        try:
            with open(ep, encoding="utf-8", errors="replace") as fh:
                declares_plugin = any(line.strip() == _PLUGIN_ENTRY_POINT_GROUP for line in fh)
        except OSError:
            continue
        if not declares_plugin:
            continue
        name, _ = artifact_name_version(entry[: -len(".dist-info")] + ".whl")
        if name:
            out[_canon(name)] = name
    return out


_SPEC_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")

#: The audit actions the two owner-only DELETE endpoints write (routers/admin_plugins.py):
#: an artifact row removed (entity_id = wheel filename) and a registry row removed
#: (entity_id = package). These rows ARE the operator's recorded intent to remove a
#: plugin; reconcile removes nothing without one.
REMOVAL_INTENT_ACTIONS = ("plugin.artifact_deleted", "plugin.unregistered")


class RemovalIntent(NamedTuple):
    """One recorded operator removal: when, which audit action, what it named,
    and who did it — copied onto the act's own audit row so the trail answers
    who / what / when without a join."""
    at: float
    action: str
    entity_id: str
    by: str | None


def spec_distribution(spec: str) -> str | None:
    """The distribution name a pip requirement spec refers to: `demoplug[browser]`,
    `demoplug>=1.0`, `demoplug == 1.0` all name `demoplug`. A registry row is
    meant to hold a bare name, but nothing enforces that, and a row that installs
    fine must never be read as "some other distribution" when desired state is
    compared with the volume."""
    m = _SPEC_NAME.match(spec or "")
    return m.group(1) if m else None


def db_is_the_apps(db: Session) -> bool:
    """True when `db` is the application's migrated database. The SQLite
    fallback (`DATABASE_URL` unset) or any other empty database answers the
    desired-state queries with zero rows; nothing below may act on zero rows
    from a database that is not the app's."""
    try:
        return db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first() is not None
    except Exception:  # noqa: BLE001 - any failure means "cannot vouch for this DB"
        with contextlib.suppress(Exception):  # a failed probe may leave the tx aborted
            db.rollback()
        return False


def recorded_removals(db: Session) -> dict[str, dict[str | None, RemovalIntent]]:
    """{canonical distribution: {version: the LATEST recorded removal}} from the
    audit trail — the rows the DELETE endpoints write. An artifact deletion
    names a wheel, so it is keyed by that wheel's version and applies only to a
    running copy of that version: tidying an old wheel's row while the current
    version stays must never become a licence to delete the current copy later.
    A registry unregister names a package with no version (key None) and
    applies to whatever copy is running.

    This, not the absence of a desired-state row, is what licenses deleting a
    plugin from the volume: absence also describes a hand-installed plugin (the
    local dev stack, 2026-09-21: two plugins on the volume, both tables empty),
    a paved database, or a restore older than an upload — none of which is an
    operator saying "remove it". Unreadable trail → empty dict → nothing removed."""
    try:
        from gdx_dispatch.core.audit import AuditLog
        rows = db.execute(
            select(AuditLog.action, AuditLog.entity_id, AuditLog.created_at, AuditLog.user_id)
            .where(AuditLog.action.in_(REMOVAL_INTENT_ACTIONS))
        ).all()
    except Exception:  # noqa: BLE001 - no trail, no intent, no removal
        log.exception("could not read the audit trail for recorded plugin removals — removing nothing")
        with contextlib.suppress(Exception):
            db.rollback()
        return {}
    out: dict[str, dict[str | None, RemovalIntent]] = {}
    for action, entity_id, created_at, user_id in rows:
        # Keyed exactly as the desired-state loops key the same values: a wheel
        # filename (artifact rows, and the filename-shaped registry rows of
        # issue #100) by its distribution AND version, a package spec by name.
        version: str | None = None
        if action == "plugin.artifact_deleted" or looks_like_artifact_filename(entity_id or ""):
            name, version = artifact_name_version(entity_id or "")
            if not version:
                continue
        else:
            name = spec_distribution(entity_id or "")
        if not name or not isinstance(created_at, datetime):
            continue
        when = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        intent = RemovalIntent(when.timestamp(), action, entity_id or "", user_id)
        per_version = out.setdefault(_canon(name), {})
        if version not in per_version or intent.at > per_version[version].at:  # the latest row wins
            per_version[version] = intent
    return out


def _applicable_removal(intents: dict[str | None, RemovalIntent], running: str | None) -> RemovalIntent | None:
    """The latest recorded removal that applies to the copy the host runs: a
    version-less unregister always does; an artifact deletion only when it names
    the RUNNING version (`effective_version`), never a stale dist-info that
    `pip --target` left lingering beside it — tidying an old wheel's row must not
    reach the current code through its leftover metadata."""
    hits = [i for v, i in intents.items()
            if v is None or (running is not None and _versions_equal(v, running))]
    return max(hits, key=lambda i: i.at) if hits else None


def _dist_info_installed_at(dist_info: str, target: str) -> float:
    """When this copy landed on the volume (the dist-info directory's mtime).
    A recorded removal must be NEWER than the copy it is applied to: a
    deletion recorded months ago must not eat a plugin someone put back since."""
    try:
        return os.path.getmtime(os.path.join(target, dist_info))
    except OSError:
        return float("inf")  # unreadable → treat as newest → no recorded removal can post-date it


def _drop_staged_wheels(canon: str, target: str, versions: set[str] | None) -> list[str]:
    """Delete the uploaded-wheel copies in `_artifacts/` that the applied
    removal actually covers: the running version when an artifact deletion
    applied, or every version of the distribution when `versions` is None (a
    registry unregister names the whole package). Wheels of versions nobody
    named stay — prod's `_artifacts/` holds seven chi_pricing wheels the
    database knows two of, and a row deletion is not a licence to erase the
    other five."""
    staged = os.path.join(target, "_artifacts")
    gone: list[str] = []
    try:
        entries = os.listdir(staged)
    except OSError:
        return gone
    for entry in entries:
        name, version = artifact_name_version(entry)
        if not name or _canon(name) != canon:
            continue
        if versions is not None and not any(_versions_equal(version or "", v) for v in versions):
            continue
        try:
            os.remove(os.path.join(staged, entry))
            gone.append(entry)
        except OSError:
            continue
    return gone


def _audit_removals(db: Session, removed: list[str], applied: dict[str, RemovalIntent]) -> None:
    """One audit row per distribution deleted from the volume. The DELETE
    endpoint recorded the operator's *intent*; this records the *act* and
    copies the intent it applied (action, what it named, who, when) onto the
    row, so who / what / when stays reconstructable without a join and after
    the container log rotates. Best-effort: a failed audit write is logged and
    never aborts the boot."""
    try:
        from gdx_dispatch.core.audit import log_audit_event_sync
        try:
            from gdx_dispatch.core.tenant import company_id
            tenant = company_id()
        except Exception:  # noqa: BLE001 - tenant resolution is not worth failing the trail over
            tenant = None
        for name in removed:
            intent = applied.get(name)
            log_audit_event_sync(
                db,
                tenant_id=tenant,
                user_id="plugin-host",
                action="plugin.removed_from_volume",
                entity_type="plugin",
                entity_id=name,
                details={
                    "reason": "operator removal recorded; no desired-state row",
                    "intent_action": intent.action if intent else None,
                    "intent_entity_id": intent.entity_id if intent else None,
                    "intent_by": intent.by if intent else None,
                    "intent_recorded_at": datetime.fromtimestamp(intent.at, tz=UTC).isoformat() if intent else None,
                    "when": "reconcile at plugin-host boot",
                },
            )
        db.commit()
    except Exception:  # noqa: BLE001 - the trail must never take the host down
        log.exception("could not write audit row(s) for removed plugin(s) %s", ", ".join(removed))
        with contextlib.suppress(Exception):
            db.rollback()


def remove_undesired_plugins(desired: set[str], intents: dict[str, dict[str | None, RemovalIntent]],
                             target: str = INSTALL_DIR,
                             applied: dict[str, RemovalIntent] | None = None) -> list[str]:
    """Delete every plugin distribution on the volume that (a) no desired-state
    row names any more AND (b) an operator recorded removing (`intents`, from
    recorded_removals) AFTER this copy was installed. Returns the names removed.

    Until 2026-09-21 nothing did this. reconcile installed what was desired and
    pruned other *versions* of it, but never subtracted what was removed — so
    DELETE on an artifact or a registry row left the package on the persistent
    volume, discovery kept finding its entry point, and the plugin ran on
    (routes mounted, event handler subscribed) while the admin page said it was
    gone. Proven on the demo stack with gdx-plugin-cellcomms.

    Why intent and not absence: a plugin on the volume with no row is also what
    a hand install, a paved database or a stale restore look like. Those are
    left in place with a warning. Ownership rules are remove_installed_dist's:
    a top-level another installed dist-info also claims is left alone.
    """
    removed: list[str] = []
    for canon, name in sorted(plugin_dists_on_volume(target).items()):
        if canon in desired:
            continue
        running = effective_version(name, target)
        intent = _applicable_removal(intents.get(canon, {}), running)
        if intent is None:
            log.warning("plugin %s is on the volume with no desired-state row and no recorded removal of "
                        "this copy — left in place (installed by hand, paved DB, restored from before its "
                        "upload, or only an older version's row was ever deleted?)", name)
            continue
        # The NEWEST dist-info is the copy that counts: a stale older dist-info
        # lingering beside it (the pip --target trap this repo has met on prod)
        # must not make a months-old deletion look newer than the current copy.
        dist_infos = _dist_info_dirs(name, target)
        installed_at = max((_dist_info_installed_at(d, target) for d in dist_infos), default=float("inf"))
        if intent.at < installed_at:
            log.warning("plugin %s: the recorded removal predates this copy of it — left in place", name)
            continue
        gone = remove_installed_dist(name, target)
        if gone:
            # Scoped by the intent that APPLIED, not by every intent ever
            # recorded: a spent year-old unregister must not widen a fresh
            # single-file deletion into "erase every staged wheel".
            versions = None if intent.action == "plugin.unregistered" else {running}
            staged = _drop_staged_wheels(canon, target, versions)
            log.info("removed plugin %s — operator removal recorded, no desired-state row (%s%s)",
                     name, ", ".join(gone), f"; staged wheels: {', '.join(staged)}" if staged else "")
            removed.append(name)
            if applied is not None:
                applied[name] = intent
        else:
            log.warning("plugin %s is no longer desired but nothing under %s could be removed", name, target)
    return removed


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
    db.execute(text(_CREATE_SQL.format(**_types_for(db))))
    db.commit()


def desired_packages(db: Session) -> list[tuple[str, str | None]]:
    rows = db.execute(
        text("SELECT package, version FROM plugin_registry ORDER BY package")
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def ensure_artifact_table(db: Session) -> None:
    db.execute(text(_ARTIFACT_SQL.format(**_types_for(db))))
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
    removed: list[str] = []
    # Canonical names of every distribution the operator wants on the volume.
    # Collected across BOTH loops below; anything installed that is not in here
    # afterwards was removed by the operator and gets deleted (see
    # remove_undesired_plugins). A failed install stays desired — it is retried
    # next boot, not thrown away.
    desired: set[str] = set()
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
                if fdist:
                    desired.add(_canon(fdist))
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
            desired.add(_canon(spec_distribution(package) or package))
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
            desired.add(_canon(adist or filename))
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
        # Both desired-state reads succeeded and this is the app's database.
        # Even then, "no row" alone never licenses a delete: only a recorded
        # operator removal (the DELETE endpoints' audit rows) newer than the
        # installed copy does. Runs last so a plugin just upgraded in place is
        # never mistaken for an orphan.
        if db_is_the_apps(db):
            applied: dict[str, RemovalIntent] = {}
            removed = remove_undesired_plugins(desired, recorded_removals(db), target=INSTALL_DIR,
                                               applied=applied)
            if removed:
                _audit_removals(db, removed, applied)
        else:
            log.error("not removing plugins: this database has no alembic_version, so it is not the app's "
                      "(DATABASE_URL unset → SQLite fallback?)")
    finally:
        if own:
            db.close()
    if INSTALL_DIR not in sys.path:
        sys.path.insert(0, INSTALL_DIR)
    if failed:
        log.error("plugin reconcile finished with %d failed spec(s): %s",
                  len(failed), ", ".join(failed))
    return ReconcileResult(installed=installed, failed=failed, removed=tuple(removed))
