"""Audit rows that say what changed (GDXA-955).

The estimate and change-order mutation handlers audited with ``details={}``.
They now write through ``core.audit.audit_best_effort`` with the handler's own
user; this builds the old/new pairs those rows carry.
"""

from __future__ import annotations


def audit_field_changes(before: dict[str, object], obj: object, sent: set[str] | frozenset[str] = frozenset()) -> dict[str, dict[str, object]]:
    """{field: {"old", "new"}} for every watched field that was sent or moved.

    ``before`` maps each watched field to its value before the mutation; the
    new value is read back off ``obj``. A field the caller sent is listed even
    when it landed on the same value, so the row shows what was asked for as
    well as what changed. Decimal/UUID/date values are stringified by the
    audit writer (core.audit._payload_json), not here.
    """
    out: dict[str, dict[str, object]] = {}
    for field, old in before.items():
        new = getattr(obj, field, None)
        if field in sent or new != old:
            out[field] = {"old": old, "new": new}
    return out
