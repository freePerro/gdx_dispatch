"""Number → customer matching, plugin-side.

Deliberately a small COPY of the semantics in
gdx_dispatch/modules/phone_com/customer_resolver.py rather than an import:
ADR-013 — a plugin importing core internals makes them public API. If the
sms-caller-identity plan ships its shared resolver into plugin_api, re-point
this module at it (and note it in both plans).

The search hash MUST equal core's ``HashColumn.hash_for_search``:
sha256 over ``SEARCH_HASH_SALT + value.lower().strip()``. Both containers
(app and plugin-host) must therefore resolve the same ``SEARCH_HASH_SALT``,
or every match silently misses — verified at deploy, asserted by test against
the real core function.

Customer reads are raw SQL on purpose (no core model import). ``name`` and
``phone_hash`` are plaintext columns (tenant_models.py notes them as such);
never widen this SELECT to an EncryptedString column — raw reads of those
return ciphertext.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

try:
    import phonenumbers

    HAS_PHONENUMBERS = True
except ImportError:
    HAS_PHONENUMBERS = False


def normalize_e164(raw: str | None, *, default_country: str = "US") -> str | None:
    """E.164 or None — same contract as phone_com's normalize_e164."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    if HAS_PHONENUMBERS:
        try:
            parsed = phonenumbers.parse(raw, default_country)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            return None
        except phonenumbers.NumberParseException as exc:
            # Never log the raw value — it's a phone number (PII).
            log.debug("cellcomms.normalize_e164_unparseable len=%d err=%s", len(raw), exc)
            return None

    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if raw.startswith("+") and re.match(r"^\+\d+$", raw):
        return raw
    return None


def search_hash(value: str) -> str:
    """sha256 hex over SEARCH_HASH_SALT + lowercased value.

    Byte-for-byte the algorithm of gdx_dispatch.core.pii.HashColumn.hash_for_search
    (pinned by test_search_hash_matches_core).
    """
    salt = os.getenv("SEARCH_HASH_SALT", "")
    return hashlib.sha256(f"{salt}{value.lower().strip()}".encode()).hexdigest()


def _canonical_uuid(raw: object) -> str | None:
    """Dashed-lowercase UUID string from whatever the driver returned —
    Postgres hands back uuid.UUID, SQLite a 32-char dashless hex string."""
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return None


def match_customer(db: Session, raw_number: str | None) -> tuple[str | None, str | None]:
    """(canonical customer id, customer name) for a number, or (None, None)."""
    e164 = normalize_e164(raw_number)
    if not e164:
        return None, None
    row = db.execute(
        text(
            "SELECT id, name FROM customers "
            "WHERE phone_hash = :h AND deleted_at IS NULL LIMIT 1"
        ),
        {"h": search_hash(e164)},
    ).first()
    if row is None:
        return None, None
    return _canonical_uuid(row[0]), row[1]


def _customer_maps(db: Session) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """One query over active customers → (phone_hash → (id, name), id → name).

    Whole-table on purpose: a per-row lookup is O(rows) queries (the 30s proxy
    timeout, re-audit finding 3), and a raw ``WHERE id = :dashed`` is the
    SQLite dashless-Uuid trap — reading id+name wholesale and canonicalizing
    in Python sidesteps both. Single-tenant customer counts make this cheap.
    """
    by_hash: dict[str, tuple[str, str]] = {}
    by_id: dict[str, str] = {}
    rows = db.execute(
        text("SELECT id, name, phone_hash FROM customers WHERE deleted_at IS NULL")
    )
    for raw_id, name, ph in rows:
        cid = _canonical_uuid(raw_id)
        if cid is None:
            continue
        by_id[cid] = name
        if ph and ph not in by_hash:
            by_hash[ph] = (cid, name)
    return by_hash, by_id


def rematch_unlinked(db: Session, company_id: str) -> dict[str, int]:
    """Retro-match: link every unlinked row, refresh linked rows' names.

    Linking exists because resolution at ingest is one-shot — a customer
    created the day AFTER their first text would otherwise stay unlinked
    forever (the hole the sms-caller-identity plan documents for phone_com).
    The name refresh keeps the denormalized ``customer_name`` snapshot honest
    after a rename. What this deliberately does NOT do: re-attribute a row
    whose number now hash-matches a DIFFERENT customer — historical events
    keep their historical link.
    """
    from gdx_plugin_cellcomms.models import CellCall, CellMessage

    by_hash, by_id = _customer_maps(db)
    linked = 0
    scanned = 0
    names_refreshed = 0
    # Cache per distinct number — one file/table repeats few numbers.
    hash_cache: dict[str, str | None] = {}

    def _hash_for(number: str | None) -> str | None:
        if number not in hash_cache:
            e164 = normalize_e164(number)
            hash_cache[number] = search_hash(e164) if e164 else None
        return hash_cache[number]

    for model in (CellMessage, CellCall):
        for r in db.query(model).filter(model.company_id == company_id):
            if r.customer_id is None:
                scanned += 1
                h = _hash_for(r.other_number)
                hit = by_hash.get(h) if h else None
                if hit:
                    r.customer_id, r.customer_name = hit
                    linked += 1
            else:
                current = by_id.get(r.customer_id)
                if current and current != r.customer_name:
                    r.customer_name = current
                    names_refreshed += 1
    db.commit()
    return {"scanned": scanned, "linked": linked, "names_refreshed": names_refreshed}
