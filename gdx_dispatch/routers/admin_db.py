"""Owner-only in-app Database admin.

Surfaces what `ssh + alembic + psql` would, behind guardrails:

* control-plane Alembic state (current rev vs head, pending, ORPHANED detection)
* tenant-plane ORM-vs-live drift (the `create_all` plane — where "model has
  column X, table lacks it" bugs live, e.g. invoices.job_id)
* safe actions: backup (pg_dump -Fc), offline SQL preview, and a guarded
  migrate-to-head (backup-gated → advisory-locked in env.py → audited).

Deliberately NOT exposed as buttons (detect-and-recommend only): re-pave,
downgrade, stamp, hand-migrate. Those stay CLI tools — a one-click data wipe or
a DDL that can take ACCESS EXCLUSIVE and stall every query is not a web action.
See migrations/versions/001_squashed_baseline.py for the re-pave path.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import gdx_dispatch.models  # noqa: F401 — registers every ORM model on TenantBase.metadata
from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync
from gdx_dispatch.core.auth_dispatcher import require_role
from gdx_dispatch.core.database import DATABASE_URL, engine, get_db
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

# DB ops are owner-only — stricter than the settings.write used by admin_ops.
router = APIRouter(
    prefix="/api/admin/db",
    tags=["admin-db"],
    dependencies=[Depends(require_role("owner"))],
)

_BACKUP_DIR = Path("/app/uploads/_db_backups")


def _alembic_cfg() -> Config:
    base = Path(__file__).resolve().parent.parent  # gdx_dispatch/
    cfg = Config(str(base / "alembic.ini"))
    cfg.set_main_option("script_location", str(base / "migrations"))
    # env.py prefers ALEMBIC_DATABASE_URL/CONTROL_DATABASE_URL; this is the fallback.
    cfg.set_main_option("sqlalchemy.url", os.getenv("ALEMBIC_DATABASE_URL") or DATABASE_URL)
    return cfg


def _render_diff(diff) -> dict | None:
    """Turn one compare_metadata tuple into a UI-friendly row, or None to skip."""
    kind = diff[0]
    # model-ahead (DB is MISSING something the ORM expects) = the dangerous kind
    if kind == "add_table":
        return {"severity": "missing", "kind": kind, "object": diff[1].name,
                "detail": f"table '{diff[1].name}' — ORM expects it, DB lacks it"}
    if kind == "add_column":
        col = diff[3]
        return {"severity": "missing", "kind": kind, "object": f"{diff[2]}.{col.name}",
                "detail": f"column '{diff[2]}.{col.name}' — ORM expects it, DB lacks it"}
    # db-ahead (DB has extra the ORM dropped) = stale, lower severity
    if kind == "remove_table":
        return {"severity": "stale", "kind": kind, "object": diff[1].name,
                "detail": f"table '{diff[1].name}' — in DB, not in ORM (stale)"}
    if kind == "remove_column":
        col = diff[3]
        return {"severity": "stale", "kind": kind, "object": f"{diff[2]}.{col.name}",
                "detail": f"column '{diff[2]}.{col.name}' — in DB, not in ORM (stale)"}
    # everything else (nullable/type/index/fk changes) = review
    obj = getattr(diff[-1], "name", None) or (str(diff[1]) if len(diff) > 1 else kind)
    return {"severity": "review", "kind": kind, "object": str(obj), "detail": f"{kind}: review"}


def _flatten_diffs(raw_diffs) -> list:
    """compare_metadata yields tuples for table-level ops and LISTS of tuples
    for column-level ops grouped per table — flatten both into single tuples."""
    flat = []
    for d in raw_diffs:
        if isinstance(d, list):
            flat.extend(d)
        else:
            flat.append(d)
    return flat


def _status_payload() -> dict:
    cfg = _alembic_cfg()
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    head = heads[0] if heads else None
    known = {s.revision for s in script.walk_revisions()}

    with engine.connect() as conn:
        mc = MigrationContext.configure(conn)
        current = mc.get_current_revision()
        raw_diffs = compare_metadata(mc, TenantBase.metadata)

    orphaned = bool(current and current not in known)
    if orphaned:
        pending = None  # can't compute a path from a revision we don't have
    elif current == head:
        pending = []
    else:
        pending = [s.revision for s in script.iterate_revisions(head, current)]

    drift = [r for d in _flatten_diffs(raw_diffs) if (r := _render_diff(d))]
    # Surface the dangerous "missing" rows first so truncation never hides them.
    _order = {"missing": 0, "stale": 1, "review": 2}
    drift.sort(key=lambda r: _order.get(r["severity"], 3))
    missing = [r for r in drift if r["severity"] == "missing"]

    latest_backup = None
    if _BACKUP_DIR.exists():
        dumps = sorted(_BACKUP_DIR.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
        if dumps:
            latest_backup = {"file": dumps[0].name, "size_bytes": dumps[0].stat().st_size,
                             "mtime": datetime.fromtimestamp(dumps[0].stat().st_mtime, UTC).isoformat()}

    return {
        "alembic": {
            "current": current,
            "head": head,
            "orphaned": orphaned,
            "pending_count": (len(pending) if pending is not None else None),
            "pending": pending,
            "at_head": (not orphaned and current == head),
        },
        "tenant_drift": {
            "total": len(drift),
            "missing_count": len(missing),  # ORM expects, DB lacks — the dangerous kind
            "items": drift[:200],
        },
        "latest_backup": latest_backup,
        # The single headline the panel acts on.
        "verdict": (
            "orphaned" if orphaned
            else "drift" if missing
            else "pending" if pending
            else "ok"
        ),
    }


@router.get("/status")
def status() -> dict:
    try:
        return _status_payload()
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the UI
        log.exception("db_admin_status_failed")
        raise HTTPException(status_code=500, detail=f"status error: {exc}") from None


@router.get("/preview")
def preview() -> dict:
    """Offline `--sql` of pending control-plane migrations — exact DDL, no apply."""
    s = _status_payload()["alembic"]
    if s["orphaned"]:
        raise HTTPException(status_code=409, detail="DB is at an orphaned revision; cannot preview. Re-pave or hand-migrate (CLI).")
    if s["at_head"]:
        return {"sql": "", "note": "Already at head — nothing pending."}
    cfg = _alembic_cfg()
    start = s["current"] or "base"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        command.upgrade(cfg, f"{start}:head", sql=True)
    return {"sql": buf.getvalue(), "from": s["current"], "to": s["head"]}


def _do_backup() -> dict:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out = _BACKUP_DIR / f"gdx_{ts}.dump"
    # List form (no shell) — pg_dump reads the connection URI directly. Path is
    # server-generated, never user input.
    r = subprocess.run(
        ["pg_dump", "-Fc", "-f", str(out), DATABASE_URL],
        capture_output=True, text=True, timeout=900,
    )
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=f"backup failed: {r.stderr[:400]}")
    return {"file": out.name, "path": str(out), "size_bytes": out.stat().st_size}


@router.post("/backup")
def backup(request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    res = _do_backup()
    log_audit_event_sync(db, action="db_backup", entity_type="database", entity_id="gdx",
                         details=res, request=request)
    db.commit()
    return res


class MigrateBody(BaseModel):
    confirm: str = Field(description="must equal 'MIGRATE'")
    skip_backup: bool = False


@router.post("/migrate")
def migrate(body: MigrateBody, request: Request,
            user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if body.confirm != "MIGRATE":
        raise HTTPException(status_code=400, detail="Type MIGRATE to confirm.")
    s = _status_payload()["alembic"]
    if s["orphaned"]:
        raise HTTPException(status_code=409, detail=(
            f"DB is at orphaned revision '{s['current']}' (not in the migration tree — likely a "
            "pre-squash install). Cannot upgrade. Re-pave or hand-migrate via CLI."))
    if s["at_head"]:
        return {"migrated": False, "note": "Already at head.", "status": _status_payload()}

    backup_info = None if body.skip_backup else _do_backup()  # gate: raises on failure

    # The advisory lock that serializes this against the entrypoint and other
    # admins is taken inside migrations/env.py (run_migrations_online).
    cfg = _alembic_cfg()
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001
        log.exception("db_admin_migrate_failed")
        log_audit_event_sync(db, action="db_migrate_failed", entity_type="database", entity_id="gdx",
                             details={"error": str(exc)[:400], "from": s["current"]}, request=request)
        db.commit()
        raise HTTPException(status_code=500, detail=f"migration failed (rolled back): {exc}") from None

    after = _status_payload()
    log_audit_event_sync(db, action="db_migrate", entity_type="database", entity_id="gdx",
                         details={"from": s["current"], "to": after["alembic"]["current"],
                                  "backup": backup_info}, request=request)
    db.commit()
    return {"migrated": True, "from": s["current"], "to": after["alembic"]["current"],
            "backup": backup_info, "status": after}
