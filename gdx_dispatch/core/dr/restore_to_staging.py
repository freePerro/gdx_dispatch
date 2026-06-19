"""SS-34 slice B — restore snapshot to staging.

:func:`restore_snapshot_to_staging` takes a
:class:`~gdx_dispatch.core.dr.backup_snapshot.SnapshotManifest`, verifies the
on-disk artifact matches the manifest ``sha256`` (constant-time
compare via :func:`hmac.compare_digest`), then invokes ``pg_restore``
against the target staging database.

Safety rules
------------

* Integrity first. We refuse to run pg_restore if the sha256 differs
  from the manifest — even by one byte. Comparison uses
  :func:`hmac.compare_digest` to prevent timing-side-channel leaks.
* Refuse to restore to a URL that LOOKS like production. The guard is
  intentionally best-effort (substring of ``prod`` or ``production``
  in the hostname portion) — the orchestrator layers a stricter check
  on top. See :func:`_refuse_if_production_like`.
* ``subprocess.run`` always uses ``check=False``, a wall-clock
  ``timeout`` (default 4h), and captures stderr.
* Row-count enumeration after restore is best-effort and bounded —
  we don't SELECT ``*`` from arbitrary tables, only ``COUNT(*)``.
  Failure to enumerate row counts is logged to ``errors`` but does
  not fail the restore itself (verification is a separate slice).
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S: int = 14_400  # 4h — RTO budget.
HASH_CHUNK_BYTES: int = 1 << 20


class RestoreError(RuntimeError):
    """Base class for restore failures."""


class IntegrityMismatchError(RestoreError):
    """The on-disk sha256 does not match the manifest."""


class ProductionTargetRefused(RestoreError):
    """The staging URL looked like production; refused to restore."""


class RestoreTimeoutError(RestoreError):
    """``pg_restore`` exceeded the configured wall-clock timeout."""


class RestoreCommandError(RestoreError):
    """``pg_restore`` exited non-zero. Carries rc + captured stderr."""

    def __init__(self, rc: int, stderr: str) -> None:
        super().__init__(f"pg_restore exit={rc}: {stderr.strip() or '(no stderr)'}")
        self.rc = rc
        self.stderr = stderr


@dataclasses.dataclass
class RestoreReport:
    """Produced by :func:`restore_snapshot_to_staging`.

    ``rows_by_table`` is an informational best-effort map populated from
    the post-restore enumeration; absence of a table does not imply
    failure (e.g. a tenant-scoped restore will omit global tables).
    """

    snapshot_id: str
    staging_db_url_redacted: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_s: float = 0.0
    integrity_verified: bool = False
    rows_by_table: dict[str, int] = dataclasses.field(default_factory=dict)
    errors: list[str] = dataclasses.field(default_factory=list)
    verification_ready_at: Optional[datetime] = None


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _redact_db_url(url: str) -> str:
    """Replace any ``user:pass@`` chunk with ``***:***@`` so the URL can
    safely land in a report or log line."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        creds, host = rest.split("@", 1)
        if ":" in creds:
            return f"{scheme}://***:***@{host}"
        return f"{scheme}://***@{host}"
    return url


def _refuse_if_production_like(staging_db_url: str) -> None:
    """Cheap guard: staging URL should not contain ``prod`` or ``production``.

    0.9-s A7: tokenize the host-region and match on whole-word boundaries
    instead of substring. The previous substring-match implementation let
    a hostname like ``myprod-test`` pass (contained ``prod-`` anywhere
    triggered the check, but a non-prod-but-prod-resembling substring
    like ``reproduction`` would also fire a false-positive — and worse,
    a ``prod-replica-staging`` host would rightly fire but a
    ``production-archive`` wouldn't match ``production`` when preceded by
    other tokens that swallowed the boundary). Token-based match is
    robust both ways.

    The orchestrator layers a stricter compare-to-DATABASE_URL check on
    top of this. Both guards exist because bugs are cheap, production
    restores are not.
    """
    lower = staging_db_url.lower()
    host_region = lower
    if "://" in host_region:
        host_region = host_region.split("://", 1)[1]
    if "@" in host_region:
        host_region = host_region.split("@", 1)[1]
    # Strip port + path so the split captures only host tokens.
    host_region = host_region.split("/", 1)[0].split(":", 1)[0]
    # Tokenize on `.` `-` `_` — the only legal hostname/DB-name separators.
    tokens = re.split(r"[.\-_]", host_region)
    banned = {"prod", "production", "prd"}
    hit = next((t for t in tokens if t in banned), None)
    if hit is not None:
        raise ProductionTargetRefused(
            f"staging_db_url looks like production "
            f"(host-token {hit!r} in {host_region!r}); refusing"
        )


def _build_pg_restore_argv(
    *,
    snapshot_path: Path,
    staging_db_url: str,
) -> list[str]:
    """Build the ``pg_restore`` argv for a clean restore into staging.

    * ``--clean --if-exists`` so a re-run of the drill on the same
      staging DB doesn't leave prior objects lying around.
    * ``--no-owner --no-privileges`` matches the pg_dump flags so
      ownership/ACL mismatches don't fail the restore.
    * ``--exit-on-error`` so we fail on the first SQL error, not
      silently after a dozen.
    """
    return [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        f"--dbname={staging_db_url}",
        str(snapshot_path),
    ]


def _enumerate_rows_by_table(
    db_exec,
    *,
    schema_filter: Optional[str] = None,
) -> tuple[dict[str, int], list[str]]:
    """Run ``SELECT COUNT(*) FROM <table>`` for every user table.

    ``db_exec`` is a callable ``(sql: str) -> iterable of rows`` so we
    can dependency-inject a stub in tests.  Errors per table are
    collected rather than raised.
    """
    errors: list[str] = []
    counts: dict[str, int] = {}
    # Intentionally conservative — information_schema query is
    # read-only, and we filter by schema when provided.
    discovery_sql = (
        "SELECT table_schema, table_name "
        "FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' "
        "AND table_schema NOT IN ('pg_catalog','information_schema')"
    )
    if schema_filter:
        # Caller-provided identifier; quote it for safety. We do not
        # allow arbitrary SQL injection via schema_filter.
        safe = schema_filter.replace("'", "''")
        discovery_sql += f" AND table_schema = '{safe}'"

    try:
        tables = list(db_exec(discovery_sql))
    except Exception as exc:  # pragma: no cover — relies on real DB
        errors.append(f"discovery failed: {exc}")
        return counts, errors

    for row in tables:
        schema, name = row[0], row[1]
        qualified = f'"{schema}"."{name}"'
        key = f"{schema}.{name}"
        try:
            rs = list(db_exec(f"SELECT COUNT(*) FROM {qualified}"))
            if rs and rs[0]:
                counts[key] = int(rs[0][0])
        except Exception as exc:
            errors.append(f"{key}: count failed: {exc}")
    return counts, errors


def restore_snapshot_to_staging(
    *,
    manifest: Any,  # SnapshotManifest — typed as Any to avoid cycle.
    staging_db_url: str,
    db_exec=None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    schema_filter: Optional[str] = None,
) -> RestoreReport:
    """Download (no-op for local path), verify, pg_restore, enumerate.

    :param manifest: a SnapshotManifest with ``backup_location`` pointing
                     to a locally-readable file. Remote URIs are the
                     caller's responsibility — this slice is local-fs.
    :param staging_db_url: target DB URL. Must not look like production.
    :param db_exec: optional callable ``(sql) -> rows`` used to count
                    rows after restore. Defaults to None (no counting).
                    The drill orchestrator supplies a SQLAlchemy-bound
                    executor.
    :param timeout_s: wall-clock timeout for pg_restore (default 4h).
    :param schema_filter: if set, restrict post-restore row-count
                          enumeration to the given schema.
    :raises ProductionTargetRefused: ``staging_db_url`` looks like prod.
    :raises IntegrityMismatchError: on-disk sha256 != manifest.sha256.
    :raises RestoreTimeoutError: pg_restore timed out.
    :raises RestoreCommandError: pg_restore exit != 0.
    """
    _refuse_if_production_like(staging_db_url)

    report = RestoreReport(
        snapshot_id=manifest.id,
        staging_db_url_redacted=_redact_db_url(staging_db_url),
        started_at=datetime.now(timezone.utc),
    )
    t0 = time.monotonic()

    path = Path(manifest.backup_location)
    if not path.exists():
        raise RestoreError(
            f"manifest.backup_location does not exist: {path}"
        )

    observed = _sha256_of_file(path)
    # Constant-time compare — never use `==` on hashes.
    if not hmac.compare_digest(
        observed.encode("ascii"), manifest.sha256.encode("ascii")
    ):
        raise IntegrityMismatchError(
            f"sha256 mismatch for {manifest.id}: "
            f"expected={manifest.sha256} observed={observed}"
        )
    report.integrity_verified = True

    argv = _build_pg_restore_argv(
        snapshot_path=path,
        staging_db_url=staging_db_url,
    )
    try:
        result = subprocess.run(  # noqa: S603 — argv is constructed
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RestoreTimeoutError(
            f"pg_restore exceeded timeout={timeout_s}s"
        ) from exc

    if result.returncode != 0:
        raise RestoreCommandError(result.returncode, result.stderr or "")

    # Best-effort row enumeration.
    if db_exec is not None:
        counts, errs = _enumerate_rows_by_table(
            db_exec, schema_filter=schema_filter
        )
        report.rows_by_table = counts
        report.errors.extend(errs)

    now = datetime.now(timezone.utc)
    report.finished_at = now
    report.duration_s = time.monotonic() - t0
    report.verification_ready_at = now
    return report
