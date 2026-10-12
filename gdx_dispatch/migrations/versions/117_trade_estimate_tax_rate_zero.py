"""Backfill tax_rate = 0 on open contractor/wholesale estimates left NULL (GDXA-756).

A NULL ``estimates.tax_rate`` is not "no tax": ``compute_estimate_totals``
(``modules/proposals/totals.py``) hands NULL to ``resolve_rate``, which returns
0 only for an exempt customer and otherwise the tenant's
``TaxConfig.default_rate`` (7.38% on prod). Garage-door work in Minnesota is a
construction contract and the customer is never charged sales tax, yet a
trade estimate created without a rate showed and billed that 7.38%. Prod
held 8 live wholesale estimates, 7 at an explicit 0 and one `sent` row at NULL
whose customer has no ``tax_exemption`` row (read-only query, 2026-10-08,
recorded in the maintainer's ledger; not re-read for this migration). This
migration fixes the rows already written. The create paths that keep writing
NULL are the sibling fix, GDXA-755, which had not landed when this shipped
(2026-10-12): a trade estimate created after this revision runs and before
that fix lands comes in at NULL again, and alembic will not rerun 117.

Existing rows, exactly:

- **Changed:** an estimate with ``tax_rate IS NULL``, ``deleted_at IS NULL``,
  ``status`` ``draft`` or ``sent``, whose customer's ``pricing_class`` is
  ``contractor`` or ``wholesale``. It gets ``tax_rate = 0`` and ``version + 1``,
  so an editor holding the old version is refused instead of saving NULL back.
  ``estimates.total`` is the pre-tax subtotal and is not touched; the tax and
  the grand total are computed on read, so from this release a `sent` row's
  public page, PDF and portal show the subtotal with tax 0. A customer who saw
  the old taxed total now sees a lower number, never a higher one.
- **Left as is, and named:** the same predicate with ``status`` ``accepted``,
  ``declined``, ``rejected`` or ``expired``. An accepted estimate is the price
  the customer agreed to, and an invoice may already exist at that price;
  rewriting it here would make the two disagree. Their ids go to the log at
  WARNING and into the audit event, for the maintainer to rule on.
- **Never touched:** invoices. An invoice copies the estimate's rate when it is
  created (``routers/invoices.py``, M24), so an invoice already issued keeps
  what it charged.
- Retail and unclassified (NULL ``pricing_class``) customers' estimates are
  outside this migration; a NULL there is the same defect class but not the
  rows the ledger described.

Explicit 0 is not an exemption record (``docs/design/
mn-tax-jurisdiction-reporting-scope.md``): it lives on the estimate, not in
``tax_exemption``, and the audit event says why it was written.

Trail: one hash-chained audit event (``estimate_tax_rate_backfill``) through
``log_audit_event_sync``, as 042 does. It flushes into this migration's
transaction and never commits, so the change and its record land together. It
carries every changed id and every left-as-is id. Nothing changed, no event
(the left-as-is rows are still logged on every run).

Portability: the predicate compares ``estimates.customer_id`` to
``customers.id``, both ``Uuid`` columns stored the same way on each engine, so
the SQLite dashless-hex trap does not apply; the downgrade re-encodes ids per
dialect. Not a row-refusing migration: it never raises on data. Rerunnable: a
second run finds no NULL rows and writes no event.

Rollback: ``downgrade()`` reads this migration's audit events and sets
``tax_rate`` back to NULL on the ids they list, only where the row still holds
0 (a rate someone set since is theirs) and is still draft or sent: one accepted
since may have an invoice at 0, and the upgrade's own line holds both ways. A
0 a person typed over the migration's 0 cannot be told apart and goes back to
NULL with the rest. That restores the 7.38% default on
those rows, i.e. the defect. By hand: take ``details.estimate_ids`` from the
``audit_logs`` row with ``action = 'estimate_tax_rate_backfill'`` and run
``UPDATE estimates SET tax_rate = NULL WHERE id IN (...) AND tax_rate = 0
AND status IN ('draft', 'sent')``.

Revision ID: 117_trade_estimate_tax_rate_zero
Revises: 116_retired_module_grants
Create Date: 2026-10-12
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from alembic import op
from sqlalchemy import bindparam, inspect, text

revision = "117_trade_estimate_tax_rate_zero"
down_revision = "116_retired_module_grants"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

ACTION = "estimate_tax_rate_backfill"
ACTOR = "migration:117_trade_estimate_tax_rate_zero"
_OPEN = ("draft", "sent")

_CANDIDATES = text(
    """
SELECT e.id, e.status, e.company_id
FROM estimates e
JOIN customers c ON c.id = e.customer_id
WHERE e.tax_rate IS NULL
  AND e.deleted_at IS NULL
  AND c.pricing_class IN ('contractor', 'wholesale')
ORDER BY e.created_at, e.id
"""
)
_ZERO = text(
    "UPDATE estimates SET tax_rate = 0, version = version + 1, updated_at = :now "
    "WHERE id = :id AND tax_rate IS NULL"
)
_EVENTS = text("SELECT details FROM audit_logs WHERE action = :action")
_RESTORE = text(
    "UPDATE estimates SET tax_rate = NULL, version = version + 1, updated_at = :now "
    "WHERE id IN :ids AND tax_rate = 0 AND status IN ('draft', 'sent')"
).bindparams(bindparam("ids", expanding=True))


def _dashed(raw) -> str:
    return str(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))


def _stored(dialect: str, dashed: str) -> str:
    """The id as this engine stores a Uuid column: SQLite keeps 32 hex."""
    return uuid.UUID(dashed).hex if dialect == "sqlite" else dashed


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    # audit_logs too: a money write with no trail is not one this makes.
    if not {"estimates", "customers", "audit_logs"} <= tables:
        return
    rows = bind.execute(_CANDIDATES).fetchall()
    changed = [r for r in rows if r.status in _OPEN]
    left = [r for r in rows if r.status not in _OPEN]
    now = datetime.now(UTC)
    for r in changed:
        bind.execute(_ZERO, {"id": r.id, "now": now})
    for r in left:
        log.warning("117: estimate %s is %s with tax_rate NULL — left as is", _dashed(r.id), r.status)
    log.info("117: %d open trade estimate(s) set to tax_rate 0, %d closed one(s) left", len(changed), len(left))
    if not changed:
        return

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    tenants = sorted({r.company_id for r in rows if r.company_id})
    log_audit_event_sync(
        session,
        tenant_id=(tenants[0] if len(tenants) == 1 else None),
        user_id=ACTOR,
        action=ACTION,
        entity_type="estimates",
        entity_id="batch",
        details={
            "from": None,
            "to": 0,
            "reason": "construction contract: never charge sales tax (GDXA-756); not an exemption",
            "estimate_ids": [_dashed(r.id) for r in changed],
            "left_as_is": {_dashed(r.id): r.status for r in left},
        },
    )
    session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"estimates", "audit_logs"} <= tables:
        return
    ids: list[str] = []
    for (details,) in bind.execute(_EVENTS, {"action": ACTION}).fetchall():
        if isinstance(details, str):
            details = json.loads(details)
        ids.extend((details or {}).get("estimate_ids") or [])
    if not ids:
        return
    dialect = bind.dialect.name
    result = bind.execute(
        _RESTORE, {"ids": [_stored(dialect, i) for i in ids], "now": datetime.now(UTC)}
    )
    log.info("117 downgrade: %d estimate(s) set back to tax_rate NULL", result.rowcount)

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    log_audit_event_sync(
        session,
        user_id=ACTOR,
        action=ACTION + "_reverted",
        entity_type="estimates",
        entity_id="batch",
        details={"estimate_ids": ids, "restored": result.rowcount},
    )
    session.flush()
