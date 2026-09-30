"""HubX order submissions — mark a job's doors ORDERED from the email.

When the office submits a cart in HubX (the door manufacturer's dealer order
portal), HubX emails an "Order submission from SubDealer …" message. Each item
in it carries the quote number, e.g.::

    Item 1
    Quantity:        1
    Quote Number:    QCD4023183
    Date Submitted:  9/28/2026 4:06:49 PM
    Job/PO Name:     xperts 89 pesta
    Model Number:    5283
    Size:            18 ft 0 in X 8 ft 0 in

That ``QCD…`` is the same number the captured door spec stores on the estimate
line (``EstimateLine.line_metadata["Number"]``), and the estimate-to-job
conversion copies it into the door's parts-needed row notes as
``Number=QCD…``. So one number reaches the job without any fuzzy matching:

    email item QCD  →  accepted estimate line  →  its job  →  that door's part row

What applying an item does, and nothing more:

1. The door's ``job_parts_needed`` row goes ``needed`` → ``ordered`` — exactly
   what the office's "Mark Ordered" button does, audited the same way. Only a
   ``needed`` row is touched; anything already ordered/received/used, or set
   by hand to something else, is left alone.
2. When no door on that job is still ``needed``, the job moves out of the
   "Order Doors" / "Need to order" holding area into "Waiting on doors" on the
   dispatch board. A job in any OTHER area (or none) is not moved — someone put
   it there on purpose.

Guards, each recorded as the row's ``outcome`` so "why didn't it move?" is
answerable from the records:

* ``no_estimate`` — no accepted estimate carries this QCD (a quote never
  converted, or ordered for stock).
* ``ambiguous`` — accepted estimates on more than one job carry it. Guessing
  would mark the wrong customer's door ordered; nothing is changed.
* ``job_closed`` — the job is completed, cancelled or deleted. Old mail must not
  rewrite history (an ``ordered`` part surfaces in the invoice checklist).
* ``nothing_needed`` — matched, but no door was still ``needed`` and the job
  was not in an order-doors lane.
* ``applied`` — something changed.

Every QCD is recorded once (``HubxDoorOrder.qcd`` is unique), so re-reading the
same message never re-flips a door the office has since set back by hand.

Only the mirrored message's 255-character preview is stored locally, and a
third of real submissions carry two or three doors — the later items are cut
off. The body is therefore fetched from Graph once per unprocessed message.
"""
from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.models.tenant_models import HoldingArea, Job, JobPartNeeded
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.vendor_orders.models import HubxDoorOrder, HubxOrderEmail

log = logging.getLogger(__name__)

HUBX_SENDER = "hubx@chiohd.com"
SUBJECT_PREFIX = "order submission"

# Holding areas an accepted estimate's job waits in until its doors are ordered
# ("Order Doors" is where estimate conversion drops it; "Need to order" is the
# office's hand-made twin), and the one it moves to once they are.
ORDER_AREAS = ("order doors", "need to order")
ORDERED_AREA = "waiting on doors"

ACTOR = "hubx-order-ingest"

# Mail older than this is not re-read: a door ordered months ago whose job is
# still open is the office's call, not an old email's.
LOOKBACK_DAYS = 30
# Each unprocessed message costs one Graph GET; bound a single sync's spend.
MAX_MESSAGES_PER_RUN = 20

# "Number=QCD4023183" inside a parts row's notes. The boundary matters:
# QCD3931957 must not match a row for QCD39319570.
_NOTES_QCD = re.compile(r"(?:^|[\s;•])Number=(QCD\d+)(?=$|[\s;•])", re.IGNORECASE)
_QCD = re.compile(r"^QCD\d+$", re.IGNORECASE)
_ITEM = re.compile(r"^Item\s+\d+$", re.IGNORECASE)
_LABEL = re.compile(r"^([A-Za-z][A-Za-z /]*?)\s*:\s*(.*)$")

ORDERED_STATUSES = ("ordered", "received", "used")
CLOSED_STAGES = ("completed", "cancelled")


@dataclass
class HubxItem:
    qcd: str
    quantity: int | None = None
    job_po_name: str | None = None
    model_number: str | None = None
    size: str | None = None
    submitted_text: str | None = None


def is_hubx_submission(from_address: str | None, subject: str | None) -> bool:
    return (
        (from_address or "").strip().lower() == HUBX_SENDER
        and (subject or "").strip().lower().startswith(SUBJECT_PREFIX)
    )


def body_to_text(content: str | None, content_type: str | None = "html") -> str:
    """Graph returns the body as HTML; the parser wants one field per line."""
    text = content or ""
    if (content_type or "").lower() == "html":
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|th|li|h\d)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = _html.unescape(text)
    return text.replace("\r", "\n")


def _int(value: str) -> int | None:
    m = re.search(r"\d+", value or "")
    return int(m.group()) if m else None


def parse_hubx_submission(text: str) -> list[HubxItem]:
    """Every item with a QCD quote number, in email order.

    Tolerates both shapes seen on real mail: ``Label:<tab>value`` on one line
    (the plain-text preview) and ``Label:`` with the value on the next line
    (the HTML body once tags are stripped).
    """
    lines = [ln.strip() for ln in (text or "").split("\n")]
    lines = [ln for ln in lines if ln]
    items: list[HubxItem] = []
    fields: dict[str, str] | None = None

    def _close() -> None:
        if fields is None:
            return
        qcd = (fields.get("quote number") or "").strip().upper()
        if not _QCD.match(qcd):
            return
        items.append(HubxItem(
            qcd=qcd,
            quantity=_int(fields.get("quantity", "")),
            job_po_name=(fields.get("job/po name") or None),
            model_number=(fields.get("model number") or None),
            size=(fields.get("size") or None),
            submitted_text=(fields.get("date submitted") or None),
        ))

    i = 0
    while i < len(lines):
        line = lines[i]
        if _ITEM.match(line):
            _close()
            fields = {}
            i += 1
            continue
        m = _LABEL.match(line)
        if m and fields is not None:
            label, value = m.group(1).strip().lower(), m.group(2).strip()
            if not value and i + 1 < len(lines):
                nxt = lines[i + 1]
                if not _ITEM.match(nxt) and not _LABEL.match(nxt):
                    value = nxt
                    i += 1
            fields.setdefault(label, value)
        i += 1
    _close()

    # One QCD once: a resubmitted cart can repeat an item.
    seen: set[str] = set()
    out: list[HubxItem] = []
    for it in items:
        if it.qcd not in seen:
            seen.add(it.qcd)
            out.append(it)
    return out


def notes_qcd(notes: str | None) -> str | None:
    m = _NOTES_QCD.search(notes or "")
    return m.group(1).upper() if m else None


def _door_rows(db: Session, job_id: str) -> list[JobPartNeeded]:
    rows = db.execute(
        select(JobPartNeeded)
        .where(JobPartNeeded.job_id == job_id)
        .where(JobPartNeeded.notes.like("%Number=QCD%"))
    ).scalars().all()
    return [r for r in rows if notes_qcd(r.notes)]


def _estimate_qcds(db: Session, job_id: Any) -> set[str]:
    """Every captured door QCD on the job's accepted estimate(s)."""
    out: set[str] = set()
    for (md,) in db.execute(
        select(EstimateLine.line_metadata)
        .join(Estimate, EstimateLine.estimate_id == Estimate.id)
        .where(Estimate.job_id == job_id)
        .where(Estimate.status == "accepted")
        .where(Estimate.deleted_at.is_(None))
    ).all():
        num = md.get("Number") if isinstance(md, dict) else None
        if isinstance(num, str) and _QCD.match(num.strip()):
            out.add(num.strip().upper())
    return out


def _job_has_unordered_door(db: Session, job: Job, doors: list[JobPartNeeded]) -> bool:
    """True while any door on this job is not yet ordered.

    A door is its QCD. It counts as ordered when its parts row has left
    ``needed``, or — for a door with NO parts row, which is every door on a
    tier-accepted estimate (the tier copy writes no ``Number=``) — when a HubX
    order for that QCD has been recorded against this job. Checking only the
    parts rows moved a two-door tier job after its first door (audit
    2026-09-29).

    A mobile-built tier estimate carries all three tiers' doors untagged, so
    the unchosen ones are never ordered and that job never moves on its own.
    That is the safe direction: the office moves it by hand.
    """
    by_qcd: dict[str, list[JobPartNeeded]] = {}
    for p in doors:
        q = notes_qcd(p.notes)
        if q:
            by_qcd.setdefault(q, []).append(p)
    if any(p.status == "needed" for p in doors):
        return True
    recorded = set(db.execute(
        select(HubxDoorOrder.qcd).where(HubxDoorOrder.matched_job_id == job.id)
    ).scalars().all())
    return any(q not in by_qcd and q not in recorded for q in _estimate_qcds(db, job.id))


def _areas_by_name(db: Session) -> dict[str, HoldingArea]:
    rows = db.execute(
        select(HoldingArea).where(HoldingArea.deleted_at.is_(None))
    ).scalars().all()
    out: dict[str, HoldingArea] = {}
    for a in rows:
        out.setdefault((a.name or "").strip().lower(), a)
    return out


def _audit(db: Session, *, entity_type: str, entity_id: str, details: dict[str, Any]) -> None:
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=ACTOR,
        action="update",
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )


def apply_hubx_item(
    db: Session,
    item: HubxItem,
    *,
    graph_message_id: str,
    received_at: datetime | None,
) -> HubxDoorOrder:
    """Apply one ordered door and record it. Flushes; the caller commits."""
    record = HubxDoorOrder(
        id=uuid4(),
        qcd=item.qcd,
        quantity=item.quantity,
        job_po_name=(item.job_po_name or None) and item.job_po_name[:200],
        model_number=(item.model_number or None) and item.model_number[:60],
        size=(item.size or None) and item.size[:60],
        submitted_text=(item.submitted_text or None) and item.submitted_text[:60],
        graph_message_id=graph_message_id,
        received_at=received_at,
        outcome="no_estimate",
        parts_marked=0,
    )
    db.add(record)
    return resolve_hubx_order(db, record)


def resolve_hubx_order(db: Session, record: HubxDoorOrder) -> HubxDoorOrder:
    """Match a recorded order to its job and apply it. Flushes; the caller commits.

    Called once when the order is first read, and again on each later sync
    while it is still ``no_estimate`` inside the lookback window
    (``retry_unmatched``) — on production, 4 of 13 matched orders were emailed
    before their estimate was accepted, three of them minutes before.
    """
    record.outcome = "no_estimate"
    graph_message_id = record.graph_message_id
    qcd = record.qcd

    hits = db.execute(
        select(Estimate.id, Estimate.job_id)
        .join(EstimateLine, EstimateLine.estimate_id == Estimate.id)
        .where(EstimateLine.line_metadata["Number"].as_string() == qcd)
        .where(Estimate.status == "accepted")
        .where(Estimate.job_id.is_not(None))
        .where(Estimate.deleted_at.is_(None))
    ).all()
    job_ids = {h.job_id for h in hits}
    if not job_ids:
        db.flush()
        return record
    if len(job_ids) > 1:
        record.outcome = "ambiguous"
        log.warning("hubx order %s matches %d jobs — left alone", qcd, len(job_ids))
        db.flush()
        return record

    job_id = next(iter(job_ids))
    record.matched_estimate_id = next(h.id for h in hits)
    record.matched_job_id = job_id
    job = db.get(Job, job_id)
    if job is None or job.deleted_at is not None or job.lifecycle_stage in CLOSED_STAGES:
        record.outcome = "job_closed"
        db.flush()
        return record

    now = datetime.now(timezone.utc)
    source = {"via": "hubx_order_email", "qcd": qcd, "graph_message_id": graph_message_id}
    doors = _door_rows(db, str(job.id))
    for part in doors:
        if notes_qcd(part.notes) != qcd or part.status != "needed":
            continue
        part.status = "ordered"
        part.updated_at = now
        record.parts_marked += 1
        _audit(db, entity_type="part_needed", entity_id=str(part.id), details={
            "status": {"from": "needed", "to": "ordered"},
            "job_id": str(job.id),
            **source,
        })

    moved = False
    db.flush()
    still_needed = _job_has_unordered_door(db, job, doors)
    if not still_needed and job.holding_area_id:
        areas = _areas_by_name(db)
        current = next(
            (a for a in areas.values() if str(a.id) == str(job.holding_area_id)), None
        )
        target = areas.get(ORDERED_AREA)
        if (
            current is not None
            and (current.name or "").strip().lower() in ORDER_AREAS
            and target is not None
        ):
            job.holding_area_id = str(target.id)
            job.updated_at = now
            record.moved_from_area = current.name
            record.moved_to_area = target.name
            moved = True
            _audit(db, entity_type="job", entity_id=str(job.id), details={
                "holding_area": {"from": current.name, "to": target.name},
                "holding_area_id": {"from": str(current.id), "to": str(target.id)},
                **source,
            })

    record.outcome = "applied" if (record.parts_marked or moved) else "nothing_needed"
    db.flush()
    return record


def retry_unmatched(db: Session, *, since: datetime) -> dict[str, int]:
    """Re-match orders recorded ``no_estimate`` since ``since``.

    The office often submits the HubX cart and then accepts the estimate, so
    the email can be read before any accepted estimate carries its QCD. The
    record keeps the QCD, so the retry needs no Graph call. Commits per record.
    """
    out = {"retried": 0, "resolved": 0, "parts_marked": 0, "jobs_moved": 0}
    pending = db.execute(
        select(HubxDoorOrder)
        .where(HubxDoorOrder.outcome == "no_estimate")
        # The window is the EMAIL's age, not the record's: a first deploy
        # records a month of backlog at once, and keying on created_at kept
        # that backlog retryable for a second month (audit round 3).
        .where(or_(
            HubxDoorOrder.received_at >= since,
            and_(HubxDoorOrder.received_at.is_(None), HubxDoorOrder.created_at >= since),
        ))
    ).scalars().all()
    for rec in pending:
        out["retried"] += 1
        try:
            resolve_hubx_order(db, rec)
            db.commit()
        except Exception:  # noqa: BLE001 — one bad record must not stop the rest
            log.exception("hubx order retry failed for %s", rec.qcd)
            db.rollback()
            continue
        if rec.outcome != "no_estimate":
            out["resolved"] += 1
            out["parts_marked"] += rec.parts_marked
            if rec.moved_to_area:
                out["jobs_moved"] += 1
    return out


def _message_key(msg: Any) -> str:
    return (getattr(msg, "internet_message_id", None) or "").strip() or f"graph:{msg.graph_message_id}"


def process_hubx_orders(
    tdb: Session,
    gc: Any,
    account: Any,
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    max_messages: int = MAX_MESSAGES_PER_RUN,
) -> dict[str, int]:
    """Read unprocessed HubX submissions from the mirror and apply them.

    Runs after a mailbox sync has committed. A message is identified by its
    Message-ID and read once: every fetched message is recorded
    (``HubxOrderEmail``), whether or not it held anything new. Each message
    commits on its own; a failure rolls back that message only, records
    nothing, and is retried next sync.
    """
    from gdx_dispatch.modules.outlook.models import OutlookMessage

    totals = {"messages": 0, "items": 0, "applied": 0, "parts_marked": 0,
              "jobs_moved": 0, "errors": 0, "retried": 0, "resolved": 0}
    since = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)

    # Orders read before their estimate was accepted get another look first.
    retry = retry_unmatched(tdb, since=since)
    totals["retried"] = retry["retried"]
    totals["resolved"] = retry["resolved"]
    totals["parts_marked"] += retry["parts_marked"]
    totals["jobs_moved"] += retry["jobs_moved"]
    candidates = tdb.execute(
        select(OutlookMessage)
        .where(OutlookMessage.account_id == account.id)
        .where(func.lower(OutlookMessage.from_address) == HUBX_SENDER)
        .where(func.lower(OutlookMessage.subject).like(SUBJECT_PREFIX + "%"))
        .where(OutlookMessage.received_at >= since)
        .order_by(OutlookMessage.received_at.asc())
    ).scalars().all()
    if not candidates:
        return totals
    done = set(tdb.execute(
        select(HubxOrderEmail.message_key).where(
            HubxOrderEmail.message_key.in_({_message_key(m) for m in candidates})
        )
    ).scalars().all())

    for msg in candidates:
        key = _message_key(msg)
        if key in done:
            continue
        if totals["messages"] >= max_messages:
            break
        totals["messages"] += 1
        try:
            full = gc.get_message(msg.graph_message_id)
            body = full.get("body") or {}
            items = parse_hubx_submission(
                body_to_text(body.get("content"), body.get("contentType"))
            )
            if not items:
                log.warning("hubx submission %s had no QCD items", msg.graph_message_id)
            known = set(tdb.execute(
                select(HubxDoorOrder.qcd).where(HubxDoorOrder.qcd.in_([i.qcd for i in items]))
            ).scalars().all()) if items else set()
            new = 0
            for item in items:
                if item.qcd in known:
                    continue
                rec = apply_hubx_item(
                    tdb, item,
                    graph_message_id=msg.graph_message_id,
                    received_at=msg.received_at,
                )
                new += 1
                totals["items"] += 1
                totals["parts_marked"] += rec.parts_marked
                if rec.outcome == "applied":
                    totals["applied"] += 1
                if rec.moved_to_area:
                    totals["jobs_moved"] += 1
            tdb.add(HubxOrderEmail(
                message_key=key,
                graph_message_id=msg.graph_message_id,
                received_at=msg.received_at,
                item_count=len(items),
                new_item_count=new,
            ))
            tdb.commit()
            done.add(key)
        except IntegrityError:
            # A concurrent sync (or the same mail in another mailbox) recorded
            # it first; its rows stand, ours are discarded.
            tdb.rollback()
            done.add(key)
        except Exception:  # noqa: BLE001 — one bad message must not stop the rest
            log.exception("hubx order ingest failed for message %s", msg.graph_message_id)
            tdb.rollback()
            totals["errors"] += 1
    return totals


def door_order_status_for_jobs(db: Session, job_ids: list[Any]) -> dict[str, dict[str, int]]:
    """{job_id: {"total": n, "ordered": m}} over the jobs' door rows.

    A door row is a parts-needed row carrying a captured door's ``Number=QCD…``.
    Jobs with no door rows are absent from the result.
    """
    keys = [str(j) for j in job_ids if j is not None]
    if not keys:
        return {}
    out: dict[str, dict[str, int]] = {}
    for row in db.execute(
        select(JobPartNeeded.job_id, JobPartNeeded.status, JobPartNeeded.notes)
        .where(JobPartNeeded.job_id.in_(keys))
        .where(JobPartNeeded.notes.like("%Number=QCD%"))
    ).all():
        if not notes_qcd(row.notes) or row.status not in ("needed", *ORDERED_STATUSES):
            continue
        agg = out.setdefault(str(row.job_id), {"total": 0, "ordered": 0})
        agg["total"] += 1
        if row.status in ORDERED_STATUSES:
            agg["ordered"] += 1
    return out
