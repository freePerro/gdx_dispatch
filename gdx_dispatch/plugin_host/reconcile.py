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
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)

INSTALL_DIR = os.getenv("PLUGIN_INSTALL_DIR", "/plugins")

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


def desired_artifacts(db: Session) -> list[tuple[str, str, bytes]]:
    """(filename, sha256, content) for every uploaded plugin artifact."""
    rows = db.execute(
        text("SELECT filename, sha256, content FROM plugin_artifact ORDER BY filename")
    ).fetchall()
    return [(r[0], r[1], bytes(r[2])) for r in rows]


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
    if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
        log.error("artifact %s sha256 mismatch — refusing to install", safe)
        return False
    staged_dir = os.path.join(target, "_artifacts")
    os.makedirs(staged_dir, exist_ok=True)
    path = os.path.join(staged_dir, safe)
    with open(path, "wb") as fh:
        fh.write(content)
    return pip_install(path, target=target)


def pip_install(spec: str, target: str = INSTALL_DIR) -> bool:
    """Install one spec into the target dir. Returns True on success, logs on
    failure (never raises — one bad package must not abort the whole boot)."""
    cmd = [sys.executable, "-m", "pip", "install", "--target", target, "--upgrade", spec]
    log.info("plugin reconcile: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("pip install failed for %s: %s", spec, (result.stderr or "")[-500:])
        return False
    return True


def reconcile(db: Session | None = None) -> list[str]:
    """Install every registry package into the volume; return the specs installed.
    Adds the install dir to sys.path so freshly-installed plugins are importable
    in this process."""
    own = db is None
    db = db or SessionLocal()
    installed: list[str] = []
    try:
        ensure_registry_table(db)
        ensure_artifact_table(db)
        for package, version in desired_packages(db):
            spec = f"{package}=={version}" if version else package
            if pip_install(spec):
                installed.append(spec)
        # Uploaded private plugins (not on any index). Verify the stored digest
        # before installing — a corrupted/tampered row won't be executed.
        for filename, sha256, content in desired_artifacts(db):
            if install_artifact(filename, content, expected_sha256=sha256):
                installed.append(filename)
    finally:
        if own:
            db.close()
    if INSTALL_DIR not in sys.path:
        sys.path.insert(0, INSTALL_DIR)
    return installed
