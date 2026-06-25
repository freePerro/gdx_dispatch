"""Owner consent for elevated plugin permissions (ADR-014).

A plugin may declare `permissions` in its manifest (e.g. "browser" — a streamed
headless browser the operator drives). Those are powerful, so before the
capability can be used, an owner must explicitly consent after reading the risk.
Consent records WHICH permissions were granted, so if a plugin later adds a new
permission the old consent doesn't silently cover it.
"""
from __future__ import annotations

import os

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session


def _plugin_host_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


def ensure_consent_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS plugin_consent (
                plugin_key   TEXT PRIMARY KEY,
                permissions  TEXT NOT NULL,
                consented_by TEXT,
                consented_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    db.commit()


def fetch_permissions(key: str) -> list[str]:
    """The permissions a plugin currently declares (from the plugin-host catalog)."""
    try:
        r = httpx.get(f"{_plugin_host_url()}/api/plugins", timeout=5.0)
        for p in r.json():
            if p.get("key") == key:
                return list(p.get("permissions") or [])
    except Exception:
        pass
    return []


def record_consent(db: Session, key: str, permissions: list[str], by: str) -> None:
    ensure_consent_table(db)
    db.execute(
        text(
            """
            INSERT INTO plugin_consent (plugin_key, permissions, consented_by)
            VALUES (:k, :p, :by)
            ON CONFLICT (plugin_key) DO UPDATE
              SET permissions = EXCLUDED.permissions,
                  consented_by = EXCLUDED.consented_by,
                  consented_at = now()
            """
        ),
        {"k": key, "p": ",".join(permissions), "by": by},
    )
    db.commit()


def consented_permissions(db: Session, key: str) -> set[str]:
    ensure_consent_table(db)
    row = db.execute(
        text("SELECT permissions FROM plugin_consent WHERE plugin_key = :k"), {"k": key}
    ).first()
    if not row or not row[0]:
        return set()
    return {p.strip() for p in row[0].split(",") if p.strip()}


def has_permission_consent(db: Session, key: str, permission: str) -> bool:
    return permission in consented_permissions(db, key)
