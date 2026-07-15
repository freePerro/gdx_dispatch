"""Idempotency keys with liveness (S3, spec §5.6).

Key form: ``{source_type}:{source_id}:{event}:{sha256(canonical fields)[:16]}:{seq}``
where **seq = count of `reversed` entries for the same (source, event, hash)
prefix** — NOT count of all entries. A plain retry must recompute the SAME
key as the live entry (and collide into idempotent success); counting all
entries would mint a fresh key on retry and double-post. Because seq derives
from ledger state, a backfill replay reconstructs identical keys in order.

The A→B→A liveness property this buys: post content A (``…hashA:0``), edit to
B (reverse, post ``…hashB:0``), edit back to A → one reversed entry exists at
the A-prefix, so the new key is ``…hashA:1`` — exactly one LIVE entry at
content A, and its key never collides with the reversed one.

Reversal entries use their own form ``reversal:{original_entry_id}`` —
unique by construction (an entry can be reversed once; the trigger makes
``reversed_by_entry_id`` write-once).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger.models import (
    ENTRY_STATUS_REVERSED,
    GlJournalEntry,
)


def _validate_canonical(value: Any, path: str = "$") -> None:
    """Hash identity must equal ECONOMIC identity, so only unambiguous JSON
    scalars may enter the canonical form. A ``default=str`` escape hatch
    would alias repr identity onto it (Decimal("10") vs "10", set ordering,
    float drift) — two different events could share a hash, or one event
    could hash differently across processes. Reject everything else loudly;
    callers canonicalize their own types (isoformat dates, str(uuid),
    int cents)."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        raise TypeError(f"float in canonical fields at {path} — pass int cents or str")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                # json.dumps silently coerces int keys to strings — {1: x}
                # would alias {"1": x}.
                raise TypeError(f"non-str mapping key {k!r} at {path}")
            _validate_canonical(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _validate_canonical(v, f"{path}[{i}]")
        return
    raise TypeError(
        f"{type(value).__name__} at {path} is not canonical-hashable — "
        "convert to str/int explicitly before hashing"
    )


def content_hash(fields: Mapping[str, Any]) -> str:
    """First 16 hex chars of sha256 over the canonical JSON of the economic
    content (sorted keys, no whitespace, JSON-native scalars only)."""
    _validate_canonical(fields)
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _validate_component(name: str, value: str) -> str:
    """Key components must keep the ':'-joined format injective (audit round
    1): a source_id like "42:issued:<hash>" would make one event's keys
    prefix-match another's counting pattern in compute_seq — seq inflates and
    a plain retry double-posts. 'reversal' is reserved as a source_type so no
    event prefix can ever match a ``reversal:{id}`` key."""
    if not value:
        raise ValueError(f"idempotency-key {name} must be non-empty")
    if ":" in value:
        raise ValueError(
            f"idempotency-key {name} may not contain ':' (got {value!r}) — "
            "compose identifiers with another separator"
        )
    return value


def key_prefix(source_type: str, source_id: str, event: str, chash: str) -> str:
    _validate_component("source_type", source_type)
    if source_type == "reversal":
        raise ValueError('source_type "reversal" is reserved for reversal keys')
    _validate_component("source_id", source_id)
    _validate_component("event", event)
    return f"{source_type}:{source_id}:{event}:{chash}"


def idempotency_key(prefix: str, seq: int) -> str:
    return f"{prefix}:{seq}"


def reversal_key(original_entry_id: UUID | str) -> str:
    return f"reversal:{original_entry_id}"


def compute_seq(session: Session, company_id: str, prefix: str) -> int:
    """seq = count of REVERSED entries whose key starts with ``prefix:``."""
    pattern = _escape_like(f"{prefix}:") + "%"
    return session.scalar(
        select(func.count())
        .select_from(GlJournalEntry)
        .where(
            GlJournalEntry.company_id == company_id,
            GlJournalEntry.status == ENTRY_STATUS_REVERSED,
            GlJournalEntry.idempotency_key.like(pattern, escape="\\"),
        )
    )


def _escape_like(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
