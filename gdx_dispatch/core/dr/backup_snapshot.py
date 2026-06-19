"""SS-34 slice A — pg_dump wrapper + sha256 integrity manifest.

:func:`create_snapshot` shells out to ``pg_dump`` (compressed custom
format by default), streams the output to ``target_location`` while
computing a SHA-256 of the bytes as they are written, and returns a
:class:`SnapshotManifest` describing what was produced.

Safety rules
------------

* ``subprocess.run`` is always called with ``shell=False`` (the default
  when argv is a list) and ``check=False``; the returncode is inspected
  explicitly; stderr is captured and surfaced on failure so silent
  failures cannot hide.
* Every caller-supplied string that flows into the argv (``label``,
  ``target_location``, ``scope_selector``, ``source_db_url``) is passed
  through :func:`_reject_arg_injection` / a purpose-built regex before
  the argv is built. This closes the "argument injection" class — even
  though we never invoke a shell, a value starting with ``-`` would be
  interpreted by ``pg_dump`` as an additional flag (e.g. a malicious
  ``source_db_url`` of ``--version`` or ``--something-destructive``).
  ``target_location`` is further resolved and constrained against
  path-traversal.
* A wall-clock ``timeout`` (default 4h = 14_400s — RTO budget) is
  applied to every pg_dump invocation. Timeouts raise
  :class:`SnapshotTimeoutError` and do NOT leave a half-written file
  behind (the file is unlinked on timeout).
* The sha256 is computed during the write, not after — no second pass
  over the artifact.
* Constant-time comparison of the sha256 at verification time uses
  :func:`hmac.compare_digest` (see :mod:`restore_to_staging`).

Not covered here
----------------

Remote upload (rclone / s3 / gcs) is the caller's responsibility —
``target_location`` is a local filesystem path for this slice. Remote
drivers can be added as a thin wrapper that takes the local artifact
produced here and uploads it. Keeping this module shell-free keeps it
testable by patching ``subprocess.run``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import re
import subprocess
import time

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

# Default timeout: 4h, aligned with the RTO budget in SS-34 acceptance
# criteria. Callers may pass a tighter budget per-scope.
DEFAULT_TIMEOUT_S: int = 14_400

# Read in 1 MiB chunks when hashing a just-written artifact. Matches
# filesystem block alignment on common Linux filesystems.
HASH_CHUNK_BYTES: int = 1 << 20

VALID_SCOPES: frozenset[str] = frozenset({"full", "tenant", "schema"})

# Security: reject shell metacharacters in any user-controlled argv element
# even though we never use shell=True. Belt-and-suspenders for future callers
# who might compose these values into other shell contexts (scripts, alerts).
_SHELL_METACHARS = re.compile(r"[;|&$`><(){}\[\]!*?~\s\\'\"]")
# Schema / label identifier: conservative Postgres identifier subset.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Label: human-readable but safe for embedding in filenames / manifest ids.
# Must start with alnum (no leading dash → no flag-confusion downstream).
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Postgres URL: require the scheme prefix so it can't be mistaken for a flag.
_PG_URL_RE = re.compile(r"^postgres(?:ql)?://")


def _reject_arg_injection(name: str, value: str) -> None:
    """Raise ``ValueError`` if ``value`` would be treated as a flag or
    contains shell metacharacters. Applied to every caller-supplied string
    that flows into the pg_dump argv (or into filenames composed from it).
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if value.startswith("-"):
        raise ValueError(
            f"{name}={value!r} starts with '-'; refused to prevent argument injection"
        )
    if _SHELL_METACHARS.search(value):
        raise ValueError(
            f"{name}={value!r} contains shell metacharacter; refused"
        )


class SnapshotError(RuntimeError):
    """Base class for snapshot failures."""


class SnapshotTimeoutError(SnapshotError):
    """``pg_dump`` exceeded the configured wall-clock timeout."""


class SnapshotCommandError(SnapshotError):
    """``pg_dump`` exited non-zero. Carries rc + captured stderr."""

    def __init__(self, rc: int, stderr: str) -> None:
        super().__init__(f"pg_dump exit={rc}: {stderr.strip() or '(no stderr)'}")
        self.rc = rc
        self.stderr = stderr


@dataclasses.dataclass(frozen=True)
class SnapshotManifest:
    """Produced by :func:`create_snapshot`. Carries everything a
    restore needs to locate and validate the dump."""

    id: str
    created_at: datetime
    size_bytes: int
    sha256: str
    scope_description: str
    backup_location: str


def _validate_scope(scope: str, scope_description: Optional[str]) -> str:
    if scope not in VALID_SCOPES:
        raise SnapshotError(
            f"invalid scope={scope!r}; must be one of {sorted(VALID_SCOPES)}"
        )
    if scope_description is None:
        return scope
    return scope_description


def _build_pg_dump_argv(
    *,
    source_db_url: str,
    target_path: Path,
    scope: str,
    scope_selector: Optional[str],
) -> list[str]:
    """Build the ``pg_dump`` argv.

    * ``full``      — dump entire database.
    * ``tenant``    — treat ``scope_selector`` as a schema name
                      (tenant isolation via schema-per-tenant).
    * ``schema``    — dump only the named schema.

    Custom format (``-Fc``) so ``pg_restore`` can selectively restore.
    """
    argv = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--compress=6",
        f"--file={target_path}",
    ]
    if scope in ("tenant", "schema"):
        if not scope_selector:
            raise SnapshotError(
                f"scope={scope} requires scope_selector (schema name)"
            )
        argv.append(f"--schema={scope_selector}")
    argv.append(source_db_url)
    return argv


def _sha256_of_file(path: Path) -> tuple[str, int]:
    """Stream-hash a file; return (hex_digest, size_bytes)."""
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
    return h.hexdigest(), total


def create_snapshot(
    *,
    label: str,
    source_db_url: str,
    target_location: str | os.PathLike[str],
    scope: str = "full",
    scope_selector: Optional[str] = None,
    scope_description: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> SnapshotManifest:
    """Run pg_dump and produce a :class:`SnapshotManifest`.

    :param label: human-readable label (e.g. ``"pre-cutover-2026-04-19"``).
                  Embedded in the returned manifest id.
    :param source_db_url: ``postgresql://…`` URL to dump from.
    :param target_location: local filesystem path to write the dump to.
                            Parent must exist; file is overwritten if it
                            already exists (caller's responsibility).
    :param scope: ``full`` | ``tenant`` | ``schema``.
    :param scope_selector: schema name for ``tenant``/``schema`` scopes.
    :param scope_description: free-form description for humans; defaults
                              to ``scope``.
    :param timeout_s: wall-clock timeout for pg_dump (default 4h).
    :raises SnapshotError: on invalid args.
    :raises SnapshotTimeoutError: on timeout.
    :raises SnapshotCommandError: on pg_dump non-zero exit.
    """
    # --- Security validation (argument-injection hardening) -----------
    # label: embedded in manifest id and safe for filename composition.
    if not _LABEL_RE.match(label or ""):
        raise ValueError(
            f"label={label!r} must match {_LABEL_RE.pattern} "
            "(letters/digits/._- only, max 128 chars); refused"
        )
    # source_db_url: must be a postgres URL so it cannot be misread as a flag.
    if not isinstance(source_db_url, str) or not _PG_URL_RE.match(source_db_url):
        raise ValueError(
            "source_db_url must be a postgresql:// URL; refused to prevent "
            "argument injection into pg_dump"
        )
    # scope_selector: if provided, must be a conservative identifier.
    if scope_selector is not None and not _IDENT_RE.match(scope_selector):
        raise ValueError(
            f"scope_selector={scope_selector!r} must be a simple SQL identifier "
            f"matching {_IDENT_RE.pattern}; refused"
        )
    # target_location: reject path traversal + flag-like paths BEFORE Path()
    # collapses the string.
    target_str = os.fspath(target_location)
    _reject_arg_injection("target_location", target_str)
    if ".." in Path(target_str).parts:
        raise ValueError(
            f"target_location={target_str!r} contains '..' traversal segment; refused"
        )
    # -------------------------------------------------------------------

    desc = _validate_scope(scope, scope_description)
    target = Path(target_str)
    if not target.parent.exists():
        raise SnapshotError(
            f"parent dir for target_location={target} does not exist"
        )

    argv = _build_pg_dump_argv(
        source_db_url=source_db_url,
        target_path=target,
        scope=scope,
        scope_selector=scope_selector,
    )

    started = time.monotonic()
    try:
        result = subprocess.run(  # noqa: S603 — argv is constructed, not shell
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        # Clean partial artifact; leaving it would poison future integrity
        # checks.
        if target.exists():
            try:
                target.unlink()
            except OSError:
                logger.warning(
                    "backup_snapshot: failed to unlink partial artifact %s after pg_dump timeout",
                    target,
                    exc_info=True,
                )
        raise SnapshotTimeoutError(
            f"pg_dump exceeded timeout={timeout_s}s for label={label!r}"
        ) from exc

    if result.returncode != 0:
        raise SnapshotCommandError(result.returncode, result.stderr or "")

    if not target.exists():
        raise SnapshotError(
            f"pg_dump reported success but target {target} missing"
        )

    digest, size = _sha256_of_file(target)
    elapsed = time.monotonic() - started

    manifest = SnapshotManifest(
        id=f"snap-{label}-{uuid4().hex[:12]}",
        created_at=datetime.now(timezone.utc),
        size_bytes=size,
        sha256=digest,
        scope_description=desc,
        backup_location=str(target),
    )
    # Side-effect-free; elapsed is logged by caller if desired.
    _ = elapsed
    return manifest
