"""SMS Backup & Restore XML backfill — both directions, full history.

Parses SyncTech's backup files (format reference:
https://shruggietech.github.io/sms-backup-restore-parser/xml-reference/ —
read 2026-09-18): ``sms.xml`` (``<smses><sms .../>``, epoch-millis ``date``,
``type`` 1=received 2=sent) and ``calls.xml`` (``<calls><call .../>``,
``type`` 1=incoming 2=outgoing 3=missed 4=voicemail 5=rejected 6=blocked).
One parser handles either file — it walks both element names. ``<mms>``
elements are counted and skipped (media messages; out of scope, reported so
the upload response says what it did NOT import).

Cross-feed dedupe: exact ``dedupe_key`` first. The webhook feed and the XML
may stamp the same event a few seconds apart (nomad's %sentStamp% vs the SMS
db ``date`` — unpinned upstream until the AVD verification runs), so a
windowed pass treats a WEBHOOK row for the same direction+number (+body for
sms) within ``NEAR_DUP_WINDOW_S`` as the same event, ranked by |stamp delta|,
each live row claimable at most once. Messages: skip the backup copy. Calls:
ENRICH the webhook row instead — nomad fires at ring time (duration 0), the
call log knows the real duration and missed/rejected outcome — and record the
backup element's key in ``backfill_key`` so the next re-upload of the same
backup recognizes it (re-audit 2026-09-18 finding 1).

Shape constraints from the same re-audit, all load-bearing:
- A FIXED number of SELECTs per import, zero per element AND zero per
  distinct number. The prod session is ``autoflush=False`` and the core proxy
  times a plugin call out at 30s; a per-element-query version took 47s on 50k
  elements and relied on autoflush for in-file duplicate visibility (an
  IntegrityError 500 in prod), and a per-distinct-number version measured
  2004 SELECTs on a 2000-number call log. All existence/near-dup/customer
  state is preloaded into memory; ``test_backfill_is_query_bounded`` pins the
  count with all-distinct numbers, the shape that breaks memoization.
- Commit once at the end; a malformed document raises ET.ParseError for the
  router, so half a file never lands.
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO

from sqlalchemy.orm import Session

from gdx_plugin_cellcomms.ingest import (
    call_dedupe_key,
    canonical_number,
    message_dedupe_key,
    parse_ts,
    ts_token,
)
from gdx_plugin_cellcomms.matching import _customer_maps, normalize_e164, search_hash
from gdx_plugin_cellcomms.models import CellCall, CellMessage

log = logging.getLogger(__name__)

NEAR_DUP_WINDOW_S = 300

_SMS_TYPE_DIRECTION = {"1": "in", "2": "out"}  # 3=draft 4=outbox 5=failed 6=queued → skipped
_CALL_TYPES = {
    "1": ("in", "incoming"),
    "2": ("out", "outgoing"),
    "3": ("in", "missed"),
    "4": ("in", "voicemail"),
    "5": ("in", "rejected"),
    "6": ("in", "blocked"),
}


def _aware(dt: datetime) -> datetime:
    """SQLite returns tz-naive datetimes even for DateTime(timezone=True)
    columns (stored-as-UTC convention); Postgres returns aware. Normalize
    before arithmetic."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class BackfillResult:
    messages_added: int = 0
    calls_added: int = 0
    calls_enriched: int = 0
    skipped_duplicates: int = 0
    skipped_other: int = 0  # drafts/outbox/unknown types/unparseable elements
    mms_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "messages_added": self.messages_added,
            "calls_added": self.calls_added,
            "calls_enriched": self.calls_enriched,
            "skipped_duplicates": self.skipped_duplicates,
            "skipped_other": self.skipped_other,
            "mms_skipped": self.mms_skipped,
            "errors": self.errors,
        }


def _closest_unclaimed(candidates, ts: datetime, claimed_ids: set) -> object | None:
    fresh = [c for c in candidates if c.id not in claimed_ids]
    if not fresh:
        return None
    within = [
        c for c in fresh
        if abs((_aware(c.sent_at if isinstance(c, CellMessage) else c.started_at) - ts).total_seconds())
        <= NEAR_DUP_WINDOW_S
    ]
    if not within:
        return None
    return min(
        within,
        key=lambda c: abs(
            (_aware(c.sent_at if isinstance(c, CellMessage) else c.started_at) - ts).total_seconds()
        ),
    )


def _attrs_json(el: ET.Element) -> str:
    # Drop the fields that are pure display duplication of `date`.
    attrs = {k: v for k, v in el.attrib.items() if k not in {"readable_date"}}
    return json.dumps(attrs, ensure_ascii=False)


def ingest_backup_xml(db: Session, company_id: str, data: bytes) -> BackfillResult:
    res = BackfillResult()

    # ── preloads: the ONLY per-import queries (see module docstring) ──
    known_msg_keys: set[str] = {
        k for (k,) in db.query(CellMessage.dedupe_key).filter(CellMessage.company_id == company_id)
    }
    call_keys_q = db.query(CellCall.dedupe_key, CellCall.backfill_key).filter(
        CellCall.company_id == company_id
    )
    known_call_keys: set[str] = set()
    for dk, bk in call_keys_q:
        known_call_keys.add(dk)
        if bk:
            known_call_keys.add(bk)
    # Live-feed rows, grouped for the windowed pass. A row already enriched
    # (backfill_key set) is spoken for and never a candidate again.
    webhook_msgs: dict[tuple[str, str], list[CellMessage]] = {}
    for m in db.query(CellMessage).filter(
        CellMessage.company_id == company_id,
        CellMessage.source == "webhook",
        CellMessage.sent_at.isnot(None),
    ):
        webhook_msgs.setdefault((m.direction, m.other_number or ""), []).append(m)
    webhook_calls: dict[tuple[str, str], list[CellCall]] = {}
    call_by_key: dict[str, CellCall] = {}
    for c in db.query(CellCall).filter(
        CellCall.company_id == company_id,
        CellCall.source == "webhook",
        CellCall.backfill_key.is_(None),
    ):
        call_by_key[c.dedupe_key] = c
        if c.started_at is not None:
            webhook_calls.setdefault((c.direction, c.other_number or ""), []).append(c)

    # Customer matching is hash-map lookup, ZERO queries per element. Texts
    # repeat few numbers but a call log is distinct-heavy (robocalls,
    # one-offs) — round-3 audit measured a per-distinct-number version at
    # 2004 SELECTs for 2000 distinct numbers, which is the 30s-proxy-timeout
    # failure wearing a different hat. One whole-table read instead.
    by_hash, _ = _customer_maps(db)
    _cust_cache: dict[str, tuple[str | None, str | None]] = {}

    def _customer(number: str) -> tuple[str | None, str | None]:
        if number not in _cust_cache:
            e164 = normalize_e164(number)
            hit = by_hash.get(search_hash(e164)) if e164 else None
            _cust_cache[number] = hit if hit else (None, None)
        return _cust_cache[number]

    claimed_msg_ids: set = set()
    claimed_call_ids: set = set()

    for _, el in ET.iterparse(BytesIO(data), events=("end",)):  # noqa: S314 — owner-authenticated, size-capped upload; expat refuses external entities
        tag = el.tag.lower()
        if tag == "mms":
            res.mms_skipped += 1
            el.clear()
            continue
        if tag == "sms":
            direction = _SMS_TYPE_DIRECTION.get(str(el.get("type") or ""))
            if direction is None:
                res.skipped_other += 1
                el.clear()
                continue
            number = canonical_number(el.get("address"))
            body = el.get("body") or ""
            ts = parse_ts(el.get("date"))
            key = message_dedupe_key(direction, number, ts_token(ts, el.get("date")), body)
            if key in known_msg_keys:
                res.skipped_duplicates += 1
                el.clear()
                continue
            if direction == "in" and ts is not None:
                near = _closest_unclaimed(
                    [m for m in webhook_msgs.get((direction, number), []) if m.body == body],
                    ts, claimed_msg_ids,
                )
                if near is not None:
                    # One live row absorbs at most ONE backup element — two
                    # identical texts minutes apart must not both collapse
                    # onto the single row the live feed caught.
                    claimed_msg_ids.add(near.id)
                    known_msg_keys.add(key)
                    res.skipped_duplicates += 1
                    el.clear()
                    continue
            cid, cname = _customer(number)
            db.add(CellMessage(
                company_id=company_id, direction=direction, other_number=number,
                body=body, sent_at=ts, customer_id=cid, customer_name=cname,
                source="backfill", dedupe_key=key, raw_payload=_attrs_json(el),
            ))
            known_msg_keys.add(key)
            res.messages_added += 1
            el.clear()
            continue
        if tag == "call":
            dir_type = _CALL_TYPES.get(str(el.get("type") or ""))
            if dir_type is None:
                res.skipped_other += 1
                el.clear()
                continue
            direction, call_type = dir_type
            number = canonical_number(el.get("number"))
            ts = parse_ts(el.get("date"))
            try:
                duration = int(str(el.get("duration") or "").strip())
            except ValueError:
                duration = None
            key = call_dedupe_key(direction, number, ts_token(ts, el.get("date")))
            # Exact-stamp hit on an un-enriched LIVE row → that row IS this
            # call, written at ring time; fall through to enrich it. Any other
            # known key (a backfill row, an enriched row's marker, an element
            # earlier in this file) is a plain duplicate.
            near = call_by_key.get(key)
            if near is None and key in known_call_keys:
                res.skipped_duplicates += 1
                el.clear()
                continue
            if near is None and direction == "in" and ts is not None:
                near = _closest_unclaimed(
                    webhook_calls.get((direction, number), []), ts, claimed_call_ids
                )
            if near is not None:
                # The call log is the authority on outcome — the webhook row
                # can't know duration or missed. Record the element's key so a
                # re-upload of this backup sees the call as already imported.
                claimed_call_ids.add(near.id)
                call_by_key.pop(near.dedupe_key, None)
                near.duration_s = duration
                near.call_type = call_type
                near.backfill_key = key
                if not near.contact_name and el.get("contact_name"):
                    near.contact_name = el.get("contact_name")
                known_call_keys.add(key)
                res.calls_enriched += 1
                el.clear()
                continue
            cid, cname = _customer(number)
            db.add(CellCall(
                company_id=company_id, direction=direction, call_type=call_type,
                other_number=number, contact_name=(el.get("contact_name") or None),
                started_at=ts, duration_s=duration, customer_id=cid, customer_name=cname,
                source="backfill", dedupe_key=key, raw_payload=_attrs_json(el),
            ))
            known_call_keys.add(key)
            res.calls_added += 1
            el.clear()
            continue
        # container tags (<smses>, <calls>) and anything unknown: ignore
        el.clear()

    db.commit()
    return res
