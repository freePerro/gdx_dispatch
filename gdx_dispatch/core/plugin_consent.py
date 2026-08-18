"""Owner consent for elevated plugin permissions (ADR-014).

A plugin may declare `permissions` in its manifest (e.g. "browser" — a streamed
headless browser the operator drives; "events"/"schedules"/"services" — automatic
execution). Those are powerful, so before the capability can be used, an owner
must explicitly consent after reading the risk. Consent records WHICH permissions
were granted, so if a plugin later adds a new permission the old consent doesn't
silently cover it.

For the event platform, consent additionally pins the plugin's declared
*automatic-execution surface*: the exact event list (the fingerprint preimage)
plus a fingerprint of (events, schedule-names, services). The core fan-out
enumerates event recipients from the STORED event list and uses the live catalog
only to detect drift — so a compromised/edited plugin-host can't expand what a
plugin receives, and a plugin upgrade that changes its declared events
fail-closes dispatch until the owner re-consents.
"""
from __future__ import annotations

import json
import logging
import os

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.events import capability_fingerprint, event_matches

log = logging.getLogger(__name__)


def _plugin_host_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


def internal_auth_headers() -> dict[str, str]:
    """Header carrying the shared internal token for plugin-host /internal/*
    calls. Empty when unset (staged rollout — plugin-host only enforces when the
    token is present, and n8n isn't on the network until Sprint 3 sets it)."""
    tok = os.getenv("GDX_INTERNAL_TOKEN", "")
    return {"X-GDX-Internal-Token": tok} if tok else {}


def ensure_consent_table(db: Session) -> None:
    # Fresh tables (tests, new deploys) get the event-platform columns inline.
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS plugin_consent (
                plugin_key           TEXT PRIMARY KEY,
                permissions          TEXT NOT NULL,
                consented_by         TEXT,
                consented_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                declared_events      TEXT,
                declared_fingerprint TEXT
            )
            """
        )
    )
    # Existing prod table predates the two columns — add them idempotently.
    # declared_events = JSON list of consented event patterns (preimage);
    # declared_fingerprint = capability_fingerprint at consent time.
    for col in ("declared_events", "declared_fingerprint"):
        try:
            db.execute(text(f"ALTER TABLE plugin_consent ADD COLUMN IF NOT EXISTS {col} TEXT"))
        except Exception:
            # Column already present, or an SQLite build without IF NOT EXISTS
            # where CREATE already made it — either way it exists now.
            log.debug("plugin_consent add-column skipped col=%s", col)
    db.commit()


def fetch_catalog() -> list[dict]:
    """The live plugin catalog from plugin-host (key/permissions/events/…)."""
    try:
        r = httpx.get(f"{_plugin_host_url()}/api/plugins", timeout=5.0)
        return list(r.json())
    except Exception:
        return []


def fetch_permissions(key: str) -> list[str]:
    """The permissions a plugin currently declares (from the plugin-host catalog)."""
    for p in fetch_catalog():
        if p.get("key") == key:
            return list(p.get("permissions") or [])
    return []


def _live_entry(key: str, catalog: list[dict] | None = None) -> dict | None:
    for p in catalog if catalog is not None else fetch_catalog():
        if p.get("key") == key:
            return p
    return None


def live_fingerprint(entry: dict) -> str:
    """capability_fingerprint of a live catalog entry (names only)."""
    return capability_fingerprint(
        events=entry.get("events") or (),
        schedule_names=entry.get("schedules") or (),
        services=entry.get("services") or (),
    )


def record_consent(db: Session, key: str, permissions: list[str], by: str) -> dict:
    """Record consent for a plugin's currently-declared permissions AND pin its
    declared event surface (preimage + fingerprint) from the live catalog."""
    ensure_consent_table(db)
    entry = _live_entry(key) or {}
    declared_events = list(entry.get("events") or [])
    fingerprint = live_fingerprint(entry) if entry else ""
    db.execute(
        text(
            """
            INSERT INTO plugin_consent
                (plugin_key, permissions, consented_by, declared_events, declared_fingerprint)
            VALUES (:k, :p, :by, :ev, :fp)
            ON CONFLICT (plugin_key) DO UPDATE
              SET permissions = EXCLUDED.permissions,
                  consented_by = EXCLUDED.consented_by,
                  consented_at = CURRENT_TIMESTAMP,
                  declared_events = EXCLUDED.declared_events,
                  declared_fingerprint = EXCLUDED.declared_fingerprint
            """
        ),
        {"k": key, "p": ",".join(permissions), "by": by,
         "ev": json.dumps(declared_events), "fp": fingerprint},
    )
    db.commit()
    return {"declared_events": declared_events, "declared_fingerprint": fingerprint}


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


def any_event_consent(db: Session) -> bool:
    """Cheap gate: is ANY plugin consented to receive events? Lets the emit path
    skip staging plugin dispatch entirely on the (common) zero-plugin box.

    READ-ONLY on purpose: this runs inside the caller's business transaction
    (the money/job choke points). It must NEVER create the table or commit —
    ensure_consent_table() commits, which would commit the caller's half-built
    invoice/job. A missing table (no plugin ever consented) just means False.

    SAVEPOINT-wrapped: plugin_consent is a lazily-created raw table (not an ORM
    model), so on a FRESH Postgres box it doesn't exist yet — a bare SELECT would
    raise UndefinedTable and POISON the caller's transaction, failing the
    invoice/job/customer commit. begin_nested() contains that abort in a
    savepoint so the outer money transaction survives. (SQLite doesn't poison, so
    this bug is Postgres-only and invisible to the SQLite test harness.)
    """
    try:
        with db.begin_nested():
            row = db.execute(
                text(
                    "SELECT 1 FROM plugin_consent "
                    "WHERE declared_events IS NOT NULL AND declared_events NOT IN ('', '[]') "
                    "LIMIT 1"
                )
            ).first()
        return row is not None
    except Exception:
        return False


def event_recipients(db: Session, event_name: str) -> tuple[list[str], list[str]]:
    """Return (recipients, drifted) for an event.

    recipients — plugin keys the owner consented to for this event, enumerated
      from the STORED declared_events preimage, and whose live catalog
      fingerprint still matches the consented one.
    drifted    — plugin keys that WOULD match but whose live fingerprint differs
      from the consented one (plugin changed its declared surface). Dispatch
      fail-closes for these; the caller raises a loud owner signal.
    """
    ensure_consent_table(db)
    rows = db.execute(
        text(
            "SELECT plugin_key, declared_events, declared_fingerprint, permissions "
            "FROM plugin_consent "
            "WHERE declared_events IS NOT NULL AND declared_events NOT IN ('', '[]')"
        )
    ).all()
    if not rows:
        return [], []
    catalog = fetch_catalog()
    recipients: list[str] = []
    drifted: list[str] = []
    for key, ev_json, stored_fp, perms in rows:
        # Defense-in-depth: the owner must have consented the 'events' permission,
        # not merely have a non-empty event list. (Manifest validation couples
        # them, but don't rely on that transitively.)
        if "events" not in {p.strip() for p in (perms or "").split(",")}:
            continue
        try:
            patterns = json.loads(ev_json) if ev_json else []
        except (json.JSONDecodeError, TypeError):
            continue
        if not event_matches(event_name, patterns):
            continue
        entry = _live_entry(key, catalog)
        if entry is None:
            continue  # plugin not currently loaded — nothing to deliver to
        if live_fingerprint(entry) != (stored_fp or ""):
            drifted.append(key)  # declared surface changed → fail closed
            continue
        recipients.append(key)
    return recipients, drifted
