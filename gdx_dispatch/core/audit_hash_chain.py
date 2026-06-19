"""SS-28 slice B — tamper-evident hash chain for platform_consumer_audit.

Every row in ``platform_consumer_audit`` carries two fields:

* ``prev_hash`` — ``row_hash`` of the previous row *for the same tenant*.
  The very first row in a tenant's chain uses 64 hex zeros as its
  ``prev_hash``.
* ``row_hash``  — ``sha256(prev_hash || canonical_json(row_minus_hash))``.

This gives a per-tenant append-only chain: mutating any row anywhere in
the chain changes its ``row_hash``, which no longer matches the
``prev_hash`` of the next row. :func:`verify_chain` walks the chain for
a tenant in insertion order and returns ``(True, -1)`` if intact or
``(False, break_index)`` identifying the FIRST row whose hash fails.

Design choices (per SS-28 spec "Rules"):

* **Canonical JSON.** :func:`canonical_json` sorts keys and emits
  ``separators=(",", ":")`` so byte-equal inputs always hash to the
  same digest. Floats and datetimes are normalized to strings; UUIDs
  become their canonical hex form.
* **SHA-256.** 64 hex chars; matches the zero-seed length.
* **``hmac.compare_digest``** for every hash comparison so an attacker
  cannot derive timing information from chain verification.
* **No side effects.** This module does NOT read or write the DB on
  its own; it builds and verifies hashes from in-memory rows or a
  caller-provided SQLAlchemy session. That keeps it safe to import
  from middleware, retention tooling, and tests alike.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping
from uuid import UUID

# Sentinel value for the first row in a tenant's chain. 64 hex chars of
# zeros so the byte-length matches a real sha256 digest — prevents a
# whole class of "empty string means tampered" confusion.
ZERO_HASH: str = "0" * 64

# Fields on a platform_consumer_audit row that participate in the hash.
# Order does NOT matter — canonical_json sorts — but keeping the list
# explicit means a future schema change can't silently start hashing a
# new column and break prior verifications.
HASHED_FIELDS: tuple[str, ...] = (
    "id",
    "tenant_id",
    "principal_identity_id",
    "action",
    "resource_type",
    "resource_id",
    "result",
    "details",
    "ip_address",
    "user_agent",
    "created_at",
    "prev_hash",
)


def _json_default(value: Any) -> Any:
    """Normalize types that json.dumps refuses by default.

    Datetimes → ISO-8601 with microseconds (so replayed canonical JSON
    is byte-stable). UUIDs → canonical hex. Decimals → str (never float,
    to avoid binary-float repr drift across platforms).
    """
    if isinstance(value, datetime):
        # Normalize to UTC, then drop tzinfo before formatting. SQLite and
        # some PG driver paths round-trip datetimes without tzinfo; PG with
        # timezone=True round-trips WITH tzinfo. Hashing the naive UTC ISO
        # form makes the digest identical in both worlds. Callers that
        # supply naive datetimes are ASSUMED to already be UTC — documented
        # in record_consumer_action (uses datetime.now(timezone.utc)).
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"audit hash: cannot serialize {type(value).__name__}")


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Return a byte-stable JSON string for a row payload.

    Keys sorted, no whitespace, ensure_ascii=True so every byte is
    7-bit safe and the digest is independent of locale.
    """
    return json.dumps(
        dict(payload),
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def compute_row_hash(row: Mapping[str, Any], prev_hash: str) -> str:
    """Compute ``row_hash`` for a row, given the previous hash.

    The row may include or omit ``prev_hash`` / ``row_hash``; this
    function extracts only HASHED_FIELDS (substituting ``prev_hash``
    with the caller-supplied value) and ignores everything else. Extra
    columns in the row do NOT change the hash — only the declared set.
    """
    if not isinstance(prev_hash, str) or len(prev_hash) != 64:
        raise ValueError("prev_hash must be 64-char sha256 hex")

    payload: dict[str, Any] = {}
    for field in HASHED_FIELDS:
        if field == "prev_hash":
            payload[field] = prev_hash
        else:
            payload[field] = row.get(field)

    serialized = canonical_json(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def hashes_equal(a: str, b: str) -> bool:
    """Constant-time compare of two hex digests.

    Wraps hmac.compare_digest so both inputs are normalized to bytes
    first. Never short-circuits on length mismatch (compare_digest
    handles that internally).
    """
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


def verify_chain(
    db: Any,
    tenant_id: str,
    *,
    rows: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[bool, int]:
    """Walk the audit chain for a tenant and report integrity.

    Returns ``(valid, break_at)``:

    * ``(True, -1)``  — chain intact (or empty).
    * ``(False, i)``  — row at index ``i`` (0-based, insertion order)
      is the first row whose stored ``row_hash`` does not match the
      recomputed value, or whose ``prev_hash`` does not match the
      previous row's ``row_hash``.

    ``rows`` may be supplied directly (tests, or callers that already
    have the set in memory). If omitted, the function loads rows via
    the SQLAlchemy session ``db``, ordering by ``created_at, id`` to
    match insertion order.
    """
    if rows is None:
        # Local import — the model lives in a stub module that may not
        # always be importable from every context (sqlite-only tests
        # that don't touch the DB shouldn't force-import it).
        from gdx_dispatch.models.platform_ss28_additions import PlatformConsumerAudit
        from uuid import UUID as _UUID

        if isinstance(tenant_id, str):
            try:
                tenant_id = _UUID(tenant_id)
            except ValueError:
                pass

        query = (
            db.query(PlatformConsumerAudit)
            .filter(PlatformConsumerAudit.tenant_id == tenant_id)
            .order_by(PlatformConsumerAudit.created_at, PlatformConsumerAudit.id)
        )
        rows = [_row_to_dict(r) for r in query.all()]
    else:
        rows = list(rows)

    expected_prev = ZERO_HASH
    for idx, row in enumerate(rows):
        stored_prev = row.get("prev_hash") or ""
        stored_row_hash = row.get("row_hash") or ""

        if not hashes_equal(stored_prev, expected_prev):
            return False, idx

        recomputed = compute_row_hash(row, expected_prev)
        if not hashes_equal(stored_row_hash, recomputed):
            return False, idx

        expected_prev = stored_row_hash

    return True, -1


def _row_to_dict(orm_row: Any) -> dict[str, Any]:
    """Extract HASHED_FIELDS + hash columns from an ORM row as a dict.

    Kept in this module so verify_chain doesn't leak SQLAlchemy details
    to its callers.
    """
    out: dict[str, Any] = {}
    for field in HASHED_FIELDS:
        out[field] = getattr(orm_row, field, None)
    out["row_hash"] = getattr(orm_row, "row_hash", None)
    return out
