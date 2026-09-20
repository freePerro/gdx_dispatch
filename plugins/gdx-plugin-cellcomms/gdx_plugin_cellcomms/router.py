"""Cell-comms plugin API. Mounted by plugin-host under /api/plugins/cellcomms.

Reachable two ways, both of which stamp the forwarded tenant headers:
  - the core proxy (authenticated users: the screens, the backfill upload,
    the rematch button);
  - the core cell-gateway webhook shim (routers/cell_gateway.py), which is the
    ONLY unauthenticated door and does its own shared-secret check before
    relaying the phone's POST to /ingest here.

List endpoints return BARE ARRAYS (host renderer assigns the response straight
to the DataTable — an {"items": [...]} envelope renders zero rows).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from sqlalchemy import or_
from sqlalchemy.orm import Session

from gdx_dispatch.plugin_api.context import PluginContext, get_plugin_context, get_plugin_db
from gdx_plugin_cellcomms.backfill import ingest_backup_xml
from gdx_plugin_cellcomms.ingest import event_to_row
from gdx_plugin_cellcomms.matching import match_customer, rematch_unlinked
from gdx_plugin_cellcomms.models import CellCall, CellMessage

log = logging.getLogger(__name__)

router = APIRouter()

MAX_BACKUP_BYTES = 100 * 1024 * 1024  # a decade of texts is tens of MB
LIST_LIMIT = 200


@router.post("/ingest")
def ingest_event(
    payload: dict,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    row = event_to_row(payload)
    if row is None:
        raise HTTPException(status_code=422, detail="unrecognized event: expected kind 'sms' or 'call'")

    kind = row.pop("kind")
    model = CellMessage if kind == "sms" else CellCall
    existing = db.query(model.id).filter(model.dedupe_key == row["dedupe_key"]).first()
    if existing is not None:
        # nomad retries with backoff — a redelivery must be a cheap no-op.
        return {"status": "duplicate", "kind": kind, "id": existing[0]}

    cid, cname = match_customer(db, row["other_number"])
    rec = model(company_id=ctx.tenant_id, customer_id=cid, customer_name=cname, **row)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return {"status": "ok", "kind": kind, "id": rec.id}


@router.post("/backfill")
async def backfill_upload(
    file: UploadFile,
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    data = await file.read()
    if len(data) > MAX_BACKUP_BYTES:
        raise HTTPException(status_code=413, detail="backup file exceeds 100 MB")
    if not data.strip():
        raise HTTPException(status_code=422, detail="empty file")
    try:
        result = ingest_backup_xml(db, ctx.tenant_id, data)
    except ET.ParseError as exc:
        db.rollback()
        # Nothing from a malformed file lands — half an import is worse than none.
        raise HTTPException(status_code=422, detail=f"not a readable SMS Backup & Restore XML file: {exc}") from exc
    # New history often belongs to customers who already exist — link it now
    # rather than waiting for the nightly pass.
    rematch = rematch_unlinked(db, ctx.tenant_id)
    return {"status": "ok", "file": file.filename, **result.as_dict(), "rematch": rematch}


@router.post("/rematch")
def rematch(
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> dict:
    return {"status": "ok", **rematch_unlinked(db, ctx.tenant_id)}


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    m, s = divmod(max(int(seconds), 0), 60)
    return f"{m}:{s:02d}"


def _fmt_ts(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/messages")
def list_messages(
    q: str | None = Query(default=None, max_length=100),
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    query = db.query(CellMessage).filter(CellMessage.company_id == ctx.tenant_id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            CellMessage.body.ilike(like),
            CellMessage.other_number.ilike(like),
            CellMessage.customer_name.ilike(like),
        ))
    rows = query.order_by(CellMessage.sent_at.desc().nullslast()).limit(LIST_LIMIT).all()
    return [
        {
            "when": _fmt_ts(r.sent_at),
            "direction": "→ out" if r.direction == "out" else "← in",
            "number": r.other_number,
            "customer": r.customer_name or "",
            "body": r.body,
        }
        for r in rows
    ]


@router.get("/calls")
def list_calls(
    q: str | None = Query(default=None, max_length=100),
    ctx: PluginContext = Depends(get_plugin_context),
    db: Session = Depends(get_plugin_db),
) -> list[dict]:
    query = db.query(CellCall).filter(CellCall.company_id == ctx.tenant_id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            CellCall.other_number.ilike(like),
            CellCall.contact_name.ilike(like),
            CellCall.customer_name.ilike(like),
        ))
    rows = query.order_by(CellCall.started_at.desc().nullslast()).limit(LIST_LIMIT).all()
    return [
        {
            "when": _fmt_ts(r.started_at),
            "type": r.call_type or ("outgoing" if r.direction == "out" else "incoming"),
            "number": r.other_number,
            "customer": r.customer_name or r.contact_name or "",
            "duration": _fmt_duration(r.duration_s),
        }
        for r in rows
    ]
