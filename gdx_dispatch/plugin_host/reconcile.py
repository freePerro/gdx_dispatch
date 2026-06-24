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

import logging
import os
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
        for package, version in desired_packages(db):
            spec = f"{package}=={version}" if version else package
            if pip_install(spec):
                installed.append(spec)
    finally:
        if own:
            db.close()
    if INSTALL_DIR not in sys.path:
        sys.path.insert(0, INSTALL_DIR)
    return installed
