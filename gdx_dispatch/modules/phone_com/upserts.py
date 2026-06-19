"""Phone.com row upsert helpers — shared between webhook receiver + backfill.

Both pc-s12 (live webhook events) and the full-resync backfill task land
the same shape of payload into the same tables. The upsert keys on the
Phone.com object id (``phone_com_call_id``, ``phone_com_message_id``,
``phone_com_voicemail_id``) so multiple writes from either path
converge to one row.

Customer resolution: every inbound row attempts ``match_caller_id`` on
the other-party phone. The blind-index hash lookup (pc-s6b) requires
``phone_hash`` to be populated on the Customer (handled by the
``@validates`` decorator on writes; backfill via pc-s6a for legacy
rows).
"""
from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from gdx_dispatch.modules.phone_com.customer_resolver import match_caller_id
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComExtension,
    PhoneComFax,
    PhoneComMessage,
    PhoneComNumber,
    PhoneComVoicemail,
)


_DIAL_OUT_RE = re.compile(r"^\s*dial[_\s]?out\s+(\+?\d+)\s*$", re.IGNORECASE)
_TYPE_PREFIX_RE = re.compile(r"^\s*type\s+", re.IGNORECASE)


def normalize_status(raw: str | None) -> tuple[str | None, str | None]:
    """Wave F / S11 — split a raw Phone.com `final_action` / `status` string
    into ``(clean_status, final_action_target)``.

    Examples:
        "type voicemail_received"   -> ("voicemail",  None)
        "voicemail received"        -> ("voicemail",  None)
        "dial_out +13202325143"     -> ("forwarded",  "+13202325143")
        "forwarded to extension 100"-> ("forwarded",  "extension 100")
        "answered"                  -> ("answered",   None)
        "completed"                 -> ("answered",   None)
        "missed" / "no_answer" / "busy" -> ("missed",  None)
        "canceled" / "hung_up"      -> ("canceled",   None)
        "" / None                   -> (None,         None)

    Tech mobile numbers and extension specifiers stop appearing in the
    status column — they live in final_action_target instead.
    """
    if not raw:
        return None, None
    s = raw.strip()
    if not s:
        return None, None
    m = _DIAL_OUT_RE.match(s)
    if m:
        return "forwarded", m.group(1)
    s_clean = _TYPE_PREFIX_RE.sub("", s).lower().strip()
    if s_clean.startswith("forward"):
        # "forwarded", "forwarded_to_extension", etc. We don't try to
        # extract the target from these — only dial_out has a confident
        # phone-number shape we know how to split.
        return "forwarded", None
    if "voicemail_received" in s_clean or "voicemail received" in s_clean or s_clean == "voicemail":
        return "voicemail", None
    if "voicemail" in s_clean:
        return "voicemail", None
    if "answered" in s_clean or s_clean == "completed":
        return "answered", None
    if "missed" in s_clean or "no_answer" in s_clean or "busy" in s_clean:
        return "missed", None
    if "canceled" in s_clean or "cancelled" in s_clean or "hung" in s_clean or "abandon" in s_clean:
        return "canceled", None
    # Unknown shape — preserve the cleaned string but not the raw target
    # (we don't know if there's a dialed number embedded). Strip "type "
    # prefix, replace underscores with spaces, truncate to fit the 40-char
    # column. No PII heuristic was confident enough to split.
    return s_clean.replace("_", " ")[:40], None


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def upsert_call(tenant_db: Session, payload: dict[str, Any]) -> PhoneComCall | None:
    """Idempotent upsert keyed on phone_com_call_id. Returns None if id missing."""
    pc_id = str(payload.get("id") or payload.get("call_id") or "")
    if not pc_id:
        return None

    row = (
        tenant_db.query(PhoneComCall)
        .filter(PhoneComCall.phone_com_call_id == pc_id)
        .first()
    )
    if row is None:
        row = PhoneComCall(phone_com_call_id=pc_id, direction="in", raw_payload={})
        tenant_db.add(row)

    direction = payload.get("direction")
    if direction in ("in", "out"):
        row.direction = direction
    row.from_number = payload.get("caller_id") or payload.get("from") or row.from_number
    row.to_number = payload.get("called_number") or payload.get("to") or row.to_number

    started = parse_iso(payload.get("start_time_epoch") or payload.get("start_time"))
    if started:
        row.started_at = started
    ended = parse_iso(payload.get("end_time_epoch") or payload.get("end_time"))
    if ended:
        row.ended_at = ended
    # Phone.com sends `call_duration` (seconds) on call-logs. Older
    # webhook shapes may use `duration` / `duration_s`; accept all.
    if "call_duration" in payload or "duration" in payload or "duration_s" in payload:
        with contextlib.suppress(TypeError, ValueError):
            row.duration_s = int(
                payload.get("call_duration")
                or payload.get("duration")
                or payload.get("duration_s")
                or 0,
            )
    # Wave F / S11: parse status / final_action through normalize_status so
    # PII (forwarded tech mobile numbers) lands in final_action_target, not
    # in the status column.
    raw_status = None
    if "status" in payload and payload.get("status") is not None:
        raw_status = str(payload.get("status"))
    elif "final_action" in payload and payload.get("final_action") is not None:
        raw_status = str(payload["final_action"])
    if raw_status is not None:
        clean, target = normalize_status(raw_status)
        row.status = clean
        row.final_action_target = target
    ext = payload.get("extension") or {}
    if isinstance(ext, dict):
        row.extension_id = str(ext.get("id")) if ext.get("id") else row.extension_id
    # Phone.com call-logs carry both an authed URL (call_recording_url)
    # and a presigned cp_url (call_recording_cp_url). Either being non-
    # empty implies a recording exists; webhook payloads sometimes use
    # `recording_url`. Persist the authed URL for primary access; cp_url
    # stays in raw_payload for the unauthed stream path.
    rec_url = (
        payload.get("call_recording_url")
        or payload.get("recording_url")
        or payload.get("call_recording_cp_url")
    )
    if rec_url:
        row.recording_url = rec_url
        row.recording_fetched_at = datetime.now(timezone.utc)

    if row.customer_id is None and row.from_number:
        cust_id = match_caller_id(tenant_db, row.from_number)
        if cust_id is not None:
            row.customer_id = cust_id

    row.raw_payload = payload
    tenant_db.commit()
    tenant_db.refresh(row)
    return row


def upsert_message(tenant_db: Session, payload: dict[str, Any]) -> PhoneComMessage | None:
    pc_id = str(payload.get("id") or payload.get("message_id") or "")
    if not pc_id:
        return None

    row = (
        tenant_db.query(PhoneComMessage)
        .filter(PhoneComMessage.phone_com_message_id == pc_id)
        .first()
    )
    if row is None:
        row = PhoneComMessage(
            phone_com_message_id=pc_id,
            thread_key="",
            direction="in",
            attachments=[],
            raw_payload={},
        )
        tenant_db.add(row)

    direction = payload.get("direction")
    if direction in ("in", "out"):
        row.direction = direction

    from_n = payload.get("from") or payload.get("from_number") or row.from_number
    to_n = payload.get("to") or payload.get("to_number") or row.to_number
    row.from_number = from_n
    row.to_number = to_n
    row.body = payload.get("text") or payload.get("body") or row.body
    if from_n and to_n:
        row.thread_key = "|".join(sorted([from_n, to_n]))

    sent = parse_iso(payload.get("created_at") or payload.get("sent_at"))
    if sent:
        row.sent_at = sent
    received = parse_iso(payload.get("received_at"))
    if received:
        row.received_at = received
    if "delivery_status" in payload:
        row.delivery_status = str(payload["delivery_status"])
    # P2.9 — Phone.com's conversation_id, when present in the payload.
    # Older webhook shapes may nest it under {"conversation": {"id": ...}};
    # accept both.
    conv_id = payload.get("conversation_id")
    if conv_id is None:
        conv = payload.get("conversation") or {}
        if isinstance(conv, dict):
            conv_id = conv.get("id")
    if conv_id is not None:
        row.phone_com_conversation_id = str(conv_id)[:80]
    if "attachments" in payload and isinstance(payload["attachments"], list):
        row.attachments = payload["attachments"]

    if row.customer_id is None:
        other = from_n if (row.direction == "in") else to_n
        if other:
            cust_id = match_caller_id(tenant_db, other)
            if cust_id is not None:
                row.customer_id = cust_id

    row.raw_payload = payload
    tenant_db.commit()
    tenant_db.refresh(row)
    return row


def upsert_voicemail(tenant_db: Session, payload: dict[str, Any]) -> PhoneComVoicemail | None:
    pc_id = str(payload.get("id") or payload.get("voicemail_id") or "")
    if not pc_id:
        return None

    row = (
        tenant_db.query(PhoneComVoicemail)
        .filter(PhoneComVoicemail.phone_com_voicemail_id == pc_id)
        .first()
    )
    if row is None:
        row = PhoneComVoicemail(phone_com_voicemail_id=pc_id, raw_payload={})
        tenant_db.add(row)

    call_ref = (
        payload.get("call_id")
        or payload.get("phone_com_call_id")
        or (payload.get("call") or {}).get("id")
    )
    if call_ref and not row.call_id:
        call = (
            tenant_db.query(PhoneComCall)
            .filter(PhoneComCall.phone_com_call_id == str(call_ref))
            .first()
        )
        if call is not None:
            row.call_id = call.id

    if "duration" in payload:
        with contextlib.suppress(TypeError, ValueError):
            row.duration_s = int(payload["duration"])
    audio = payload.get("audio_url") or payload.get("voicemail_url")
    if audio:
        row.audio_url = audio
        row.audio_fetched_at = datetime.now(timezone.utc)
    if "transcript" in payload or "voicemail_transcript" in payload:
        row.transcript = payload.get("transcript") or payload.get("voicemail_transcript")
        row.transcript_source = "phone_com"
    ext = payload.get("extension") or {}
    if isinstance(ext, dict) and ext.get("id"):
        row.extension_id = str(ext["id"])

    row.raw_payload = payload
    tenant_db.commit()
    tenant_db.refresh(row)
    return row


def upsert_fax(tenant_db: Session, payload: dict[str, Any]) -> PhoneComFax | None:
    """P2.7 — idempotent upsert keyed on Phone.com fax id."""
    pc_id = str(payload.get("id") or payload.get("fax_id") or "")
    if not pc_id:
        return None
    row = (
        tenant_db.query(PhoneComFax)
        .filter(PhoneComFax.phone_com_fax_id == pc_id)
        .first()
    )
    if row is None:
        row = PhoneComFax(phone_com_fax_id=pc_id, direction="in", raw_payload={})
        tenant_db.add(row)
    direction = payload.get("direction")
    if direction in ("in", "out"):
        row.direction = direction
    row.from_number = payload.get("from") or payload.get("from_number") or row.from_number
    row.to_number = payload.get("to") or payload.get("to_number") or row.to_number
    if "pages" in payload:
        with contextlib.suppress(TypeError, ValueError):
            row.pages = int(payload["pages"])
    if "status" in payload:
        row.status = str(payload["status"])[:40]
    received = parse_iso(
        payload.get("received_at") or payload.get("created_at") or payload.get("sent_at")
    )
    if received:
        row.received_at = received
    pdf = payload.get("pdf_url") or payload.get("download_url") or payload.get("url")
    if pdf:
        row.pdf_url = pdf
    if row.customer_id is None:
        other = row.from_number if row.direction == "in" else row.to_number
        if other:
            cust_id = match_caller_id(tenant_db, other)
            if cust_id is not None:
                row.customer_id = cust_id
    row.raw_payload = payload
    tenant_db.commit()
    tenant_db.refresh(row)
    return row


def upsert_extension(
    tenant_db: Session, payload: dict[str, Any]
) -> PhoneComExtension | None:
    """Idempotent upsert keyed on Phone.com extension id (Wave C / S4)."""
    pc_id = str(payload.get("id") or payload.get("extension_id") or "")
    if not pc_id:
        return None

    row = (
        tenant_db.query(PhoneComExtension)
        .filter(PhoneComExtension.phone_com_extension_id == pc_id)
        .first()
    )
    if row is None:
        row = PhoneComExtension(phone_com_extension_id=pc_id)
        tenant_db.add(row)

    name = payload.get("name") or payload.get("display_name")
    if name:
        row.name = str(name)[:200]
    number = payload.get("number") or payload.get("extension")
    if number is not None:
        row.number = str(number)[:40]
    if "is_active" in payload:
        row.is_active = bool(payload["is_active"])
    row.last_synced_at = datetime.now(timezone.utc)

    tenant_db.commit()
    tenant_db.refresh(row)
    return row


def upsert_phone_number(
    tenant_db: Session, payload: dict[str, Any]
) -> PhoneComNumber | None:
    """Idempotent upsert keyed on the phone number itself (Wave C / S3)."""
    pc_num = (
        payload.get("phone_number")
        or payload.get("number")
        or payload.get("e164")
        or payload.get("phone_com_number")
    )
    if not pc_num:
        return None
    pc_num = str(pc_num)[:40]

    row = (
        tenant_db.query(PhoneComNumber)
        .filter(PhoneComNumber.phone_com_number == pc_num)
        .first()
    )
    if row is None:
        row = PhoneComNumber(phone_com_number=pc_num)
        tenant_db.add(row)

    label = payload.get("name") or payload.get("label")
    if label:
        row.label = str(label)[:200]
    if "is_default_outbound" in payload:
        row.is_default_outbound = bool(payload["is_default_outbound"])
    row.last_synced_at = datetime.now(timezone.utc)

    tenant_db.commit()
    tenant_db.refresh(row)
    return row
