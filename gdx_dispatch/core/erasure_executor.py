"""SS-35 slice E — category-rule-driven PII scrub executor.

Walks :mod:`gdx_dispatch.core.pii_registry` for every PII field associated with a
target identity and writes a scrubbed value per category rule.

Category default strategies
---------------------------

- ``contact``     → ``"erased"``  (e.g. email becomes ``"[ERASED]"``)
- ``identity``    → ``"erased"``
- ``financial``   → ``"skip"``    (ledger immutability — Art. 17(3)(e))
- ``health``      → ``"null"``
- ``location``    → ``"null"``
- ``behavioral``  → ``"null"``
- ``technical``   → ``"null"``

Individual registrations may override with ``scrub_strategy``.

Dry-run rules (MANDATORY)
-------------------------

When ``dry_run=True`` the function MUST NOT write and MUST NOT return
plaintext values — only per-category counts. This is the only safe way
to preview an erasure without leaking the very data you're about to
scrub.

Return shape
------------

::

    {
      "dry_run": bool,
      "target_identity_id": "...",
      "by_category": { "contact": 3, "identity": 1, ... },
      "affected_field_count": N,
      "skipped_field_count": M,
      "executed_at": "<iso, only if dry_run=False>"
    }
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import text

from gdx_dispatch.core.pii_registry import (
    VALID_CATEGORIES,
    get_pii_for_identity,
    list_pii_fields,
)

logger = logging.getLogger(__name__)


# Category → default scrub strategy.
CATEGORY_DEFAULT_STRATEGY: Dict[str, str] = {
    "contact": "erased",
    "identity": "erased",
    "financial": "skip",
    "health": "null",
    "location": "null",
    "behavioral": "null",
    "technical": "null",
}


ERASED_LITERAL = "[ERASED]"


def _effective_strategy(rec_scrub_override: str | None, category: str) -> str:
    if rec_scrub_override is not None:
        return rec_scrub_override
    return CATEGORY_DEFAULT_STRATEGY.get(category, "null")


def _scrub_value(strategy: str) -> Any:
    if strategy == "erased":
        return ERASED_LITERAL
    if strategy == "null":
        return None
    raise ValueError(f"refusing to scrub for strategy={strategy!r}")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute_erasure(
    db: Any, identity_id: str, dry_run: bool = True
) -> Dict[str, Any]:
    """Scrub all registered PII fields for ``identity_id``.

    :param dry_run: If True (default), return counts only — no write.
    """
    # Initialise zero counts for every category so the summary is
    # always complete — missing categories = incomplete erasure plan.
    by_category: Dict[str, int] = {c: 0 for c in VALID_CATEGORIES}
    affected = 0
    skipped = 0

    # Reverse-lookup from (table, column) → registration so we can find
    # scrub_strategy and identity_fk_column per field.
    reg_index = {(r.table, r.column): r for r in list_pii_fields()}

    rows = get_pii_for_identity(db, identity_id)
    # Group updates by (table, row_id) so we issue one UPDATE per row,
    # not one per column — less noisy and keeps composite semantics.
    pending: Dict[tuple, Dict[str, Any]] = {}
    fk_columns: Dict[str, str] = {}

    for rec in rows:
        cat = rec["pii_category"]
        reg = reg_index.get((rec["table"], rec["column"]))
        strategy = _effective_strategy(
            reg.scrub_strategy if reg else None, cat
        )
        if strategy == "skip":
            skipped += 1
            continue

        affected += 1
        by_category[cat] = by_category.get(cat, 0) + 1

        if dry_run:
            continue

        # Resolve fk column once per table
        if rec["table"] not in fk_columns:
            if reg and reg.identity_fk_column:
                fk_columns[rec["table"]] = reg.identity_fk_column
            elif rec["table"] == "identities":
                fk_columns[rec["table"]] = "id"
            else:
                fk_columns[rec["table"]] = "identity_id"

        key = (rec["table"], rec.get("row_id"))
        pending.setdefault(key, {})[rec["column"]] = _scrub_value(strategy)

    summary: Dict[str, Any] = {
        "dry_run": dry_run,
        "target_identity_id": str(identity_id),
        "by_category": by_category,
        "affected_field_count": affected,
        "skipped_field_count": skipped,
    }

    if dry_run:
        # DRY-RUN MUST NOT leak values — explicit contract.
        return summary

    # Execute writes. Each row becomes one UPDATE statement.
    for (table, row_id), col_updates in pending.items():
        fk_col = fk_columns[table]
        set_clause = ", ".join(f"{c} = :v_{c}" for c in col_updates)
        # Bind values with prefixed names to avoid collision with :iid.
        params: Dict[str, Any] = {f"v_{c}": v for c, v in col_updates.items()}
        if table == "identities":
            params["iid"] = identity_id
            stmt = text(
                f"UPDATE {table} SET {set_clause} WHERE {fk_col} = :iid"
            )
        else:
            params["iid"] = identity_id
            params["rid"] = row_id
            stmt = text(
                f"UPDATE {table} SET {set_clause} "
                f"WHERE {fk_col} = :iid AND id = :rid"
            )
        db.execute(stmt, params)

    summary["executed_at"] = _utcnow_iso()
    return summary
