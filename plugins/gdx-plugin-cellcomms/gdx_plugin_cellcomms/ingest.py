"""Normalize one android-nomad-gateway webhook event into a row dict.

The phone is configured (see README) to POST exactly these template shapes:

  {"kind":"sms","from":"%from%","text":"%text%","sentStamp":"%sentStamp%",
   "receivedStamp":"%receivedStamp%","sim":"%sim%"}
  {"kind":"call","from":"%from%","contact":"%contact%","timestamp":"%timestamp%",
   "duration":"%duration%"}

Nomad forwards INCOMING events only, so direction is always "in" here; the
backfill (backfill.py) owns outgoing history. Timestamps arrive as epoch millis
in practice but the parser is liberal (epoch s/ms, ISO-8601) — a stamp we can't
read becomes NULL sent_at, never a dropped event.

Dedupe keys hash message-intrinsic fields at SECOND granularity, in the exact
format backfill.py uses, so a later XML backfill of the same message/call is a
no-op instead of a duplicate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from gdx_plugin_cellcomms.matching import normalize_e164


def parse_ts(value: Any) -> datetime | None:
    """Epoch seconds/millis (int or digit-string) or ISO-8601 → aware UTC."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lstrip("-").isdigit():
        try:
            n = int(s)
        except ValueError:
            return None
        if abs(n) > 100_000_000_000:  # millis, not seconds
            n = n // 1000
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ts_token(dt: datetime | None, raw: object = None) -> str:
    """The timestamp part of a dedupe key: epoch seconds when parseable, else
    the RAW stamp string. Without the fallback, two DIFFERENT texts with
    unreadable stamps (same number, same body) hash to one key and silently
    collapse — audit finding, 2026-09-18. The raw string keeps a redelivery of
    the same event stable while keeping distinct events distinct."""
    if dt is not None:
        return str(int(dt.timestamp()))
    return str(raw or "").strip()


def message_dedupe_key(direction: str, number: str, ts: str, body: str) -> str:
    payload = f"sms|{direction}|{number}|{ts}|{body or ''}"
    return hashlib.sha256(payload.encode()).hexdigest()


def call_dedupe_key(direction: str, number: str, ts: str) -> str:
    payload = f"call|{direction}|{number}|{ts}"
    return hashlib.sha256(payload.encode()).hexdigest()


def canonical_number(raw: str | None) -> str:
    """E.164 when parseable, else the raw string — one rule for storage AND
    dedupe keys, so webhook and backfill spellings of a number converge."""
    return normalize_e164(raw) or (raw or "").strip()


def event_to_row(payload: dict) -> dict | None:
    """Nomad event → row dict for CellMessage/CellCall, or None if not one.

    Returns {"kind": "sms"|"call", **column values}. Raises nothing on odd
    payloads — an unrecognized kind is None (the router 422s it), missing
    fields degrade to NULL columns.
    """
    kind = str(payload.get("kind") or "").strip().lower()
    number = canonical_number(payload.get("from"))
    raw_json = json.dumps(payload, ensure_ascii=False)

    if kind == "sms":
        sent_at = parse_ts(payload.get("sentStamp")) or parse_ts(payload.get("receivedStamp"))
        body = str(payload.get("text") or "")
        token = ts_token(sent_at, payload.get("sentStamp") or payload.get("receivedStamp"))
        return {
            "kind": "sms",
            "direction": "in",
            "other_number": number,
            "body": body,
            "sim": (str(payload.get("sim")) or None) if payload.get("sim") is not None else None,
            "sent_at": sent_at,
            "source": "webhook",
            "dedupe_key": message_dedupe_key("in", number, token, body),
            "raw_payload": raw_json,
        }

    if kind == "call":
        started_at = parse_ts(payload.get("timestamp"))
        try:
            duration = int(str(payload.get("duration")).strip())
        except (ValueError, TypeError, AttributeError):
            duration = None
        contact = str(payload.get("contact") or "").strip() or None
        return {
            "kind": "call",
            "direction": "in",
            "call_type": "incoming",
            "other_number": number,
            "contact_name": contact,
            "started_at": started_at,
            "duration_s": duration,
            "source": "webhook",
            "dedupe_key": call_dedupe_key("in", number, ts_token(started_at, payload.get("timestamp"))),
            "raw_payload": raw_json,
        }

    return None
