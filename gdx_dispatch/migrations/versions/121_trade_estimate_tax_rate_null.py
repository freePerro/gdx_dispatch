"""Undo 117's retired "trade customers are never taxed" rule on open service-lane estimates (GDXA-907).

117 (GDXA-756) wrote ``tax_rate = 0`` on open contractor/wholesale estimates
because "garage-door work is a construction contract and is never taxed".
Doug's ruling of 2026-10-13 retired that: a trade customer is taxed at the
tenant default unless a ``tax_exemption`` row says otherwise, and only an
installation (``tax.service.is_install_lane``) is untaxed. A stored 0 is not an
exemption, and on a non-install job the invoice path bills the quoted 0
(``resolve_rate_for_job``, M24/GDXA-842), so those rows under-charge tax. This
migration sets them back to NULL, which resolves to the tenant default on read
(``compute_estimate_totals``) and on invoice create.

Not every 0 is the defect. GDXA-755 (``proposals.totals.new_estimate_tax_rate``)
stores 0 on purpose for an estimate on no job or on an install-lane job, and a
NULL estimate rate resolves to the default *without* a lane check. NULL-ing
those would show tax on an installation quote while its invoice still billed 0.
So the lane is part of the predicate. It is read through the live
``is_install_lane``, not a frozen copy, on purpose: the row this leaves at 0 must
be the row today's create path would store at 0. A later change to that
function changes what a database that has not yet reached this revision gets.

Existing rows, exactly. In scope: ``tax_rate = 0``, ``deleted_at IS NULL``,
customer ``pricing_class`` ``contractor`` or ``wholesale``. Of those:

- **Changed** to ``tax_rate = NULL``, ``version + 1``: the estimate's job
  exists and is not install-lane, the status is ``draft`` or ``sent``, and
  either its id is in 117's ``estimate_tax_rate_backfill`` event (the 0 was the
  migration's, not a person's: put back whatever the customer's exemption
  record says, since 117 never read it and NULL resolves an active exemption
  to 0 anyway), or it is a ``draft`` whose customer has no ``tax_exemption``
  row. ``estimates.total`` is the pre-tax subtotal and is not touched; tax and
  grand total are computed on read, so a changed `sent` row's public page shows
  the taxed total again, as it did before 117.
- **Left, named at WARNING and in the audit event, for the maintainer:** a
  ``sent`` row whose 0 117 did not write (it may be a deliberate quote the
  customer is holding; raising it silently is not this migration's call), and
  any ``accepted``, ``declined``, ``rejected`` or ``expired`` row (the agreed
  price; an invoice may exist at it).
- **Left, counted at INFO and named in the event:** a 0 117 did not write on
  an estimate whose customer has a ``tax_exemption`` row (any row, active or
  not: the exemption record is the maintainer's to judge), and an
  estimate on no job, a job id that matches no job, or an install-lane job
  (0 is the GDXA-755 rate).
- Retail and unclassified customers: out of scope, as in 117. Invoices: never
  touched; an issued invoice keeps what it charged.

Trail: one hash-chained audit event (``estimate_tax_rate_null_backfill``)
through ``log_audit_event_sync``, flushed into this migration's transaction, so
the change and its record land together. Nothing changed, no event.

Portability: every comparison is Uuid column to Uuid column, stored the same
way on each engine, so the SQLite dashless-hex trap does not apply; the
downgrade re-encodes the logged ids per dialect. No literal ``%``. Never raises
on data. Rerunnable: a second run finds no draft (or 117-written sent) 0 left.

Rollback: ``downgrade()`` reads this migration's events and sets ``tax_rate``
back to 0 on the ids they list, only where the row is still NULL and still
draft or sent. By hand: take ``details.estimate_ids`` from the ``audit_logs``
row with ``action = 'estimate_tax_rate_null_backfill'`` and run
``UPDATE estimates SET tax_rate = 0, version = version + 1 WHERE id IN (...)
AND tax_rate IS NULL AND status IN ('draft', 'sent')`` (ids as 32 hex on
SQLite).

Revision ID: 121_trade_estimate_tax_rate_null
Revises: 120_completed_jobs_dispatch_done
Create Date: 2026-10-13
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from alembic import op
from sqlalchemy import bindparam, inspect, text

revision = "121_trade_estimate_tax_rate_null"
down_revision = "120_completed_jobs_dispatch_done"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

ACTION = "estimate_tax_rate_null_backfill"
ACTOR = "migration:121_trade_estimate_tax_rate_null"
ACTION_117 = "estimate_tax_rate_backfill"
_OPEN = ("draft", "sent")

_CANDIDATES = text(
    """
SELECT e.id, e.status, e.company_id, e.job_id,
       j.id AS found_job_id, j.job_type,
       EXISTS (SELECT 1 FROM tax_exemption x WHERE x.customer_id = e.customer_id) AS has_exemption
FROM estimates e
JOIN customers c ON c.id = e.customer_id
LEFT JOIN jobs j ON j.id = e.job_id
WHERE e.tax_rate = 0
  AND e.deleted_at IS NULL
  AND c.pricing_class IN ('contractor', 'wholesale')
ORDER BY e.created_at, e.id
"""
)
_NULL = text(
    "UPDATE estimates SET tax_rate = NULL, version = version + 1, updated_at = :now "
    "WHERE id = :id AND tax_rate = 0"
)
_EVENTS = text("SELECT details FROM audit_logs WHERE action = :action")
_RESTORE = text(
    "UPDATE estimates SET tax_rate = 0, version = version + 1, updated_at = :now "
    "WHERE id IN :ids AND tax_rate IS NULL AND status IN ('draft', 'sent')"
).bindparams(bindparam("ids", expanding=True))


def _dashed(raw) -> str:
    return str(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))


def _stored(dialect: str, dashed: str) -> str:
    """The id as this engine stores a Uuid column: SQLite keeps 32 hex."""
    return uuid.UUID(dashed).hex if dialect == "sqlite" else dashed


def _logged_ids(bind, action: str) -> list[str]:
    ids: list[str] = []
    for (details,) in bind.execute(_EVENTS, {"action": action}).fetchall():
        if isinstance(details, str):
            details = json.loads(details)
        ids.extend((details or {}).get("estimate_ids") or [])
    return ids


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    # tax_exemption too: without it no row can be shown to lack an exemption.
    if not {"estimates", "customers", "jobs", "tax_exemption", "audit_logs"} <= tables:
        return

    from gdx_dispatch.modules.tax.service import is_install_lane

    by_117 = {_dashed(i) for i in _logged_ids(bind, ACTION_117)}
    changed, sent_left, closed_left, exempt_left, lane_left = [], {}, {}, [], []
    for r in bind.execute(_CANDIDATES).fetchall():
        eid = _dashed(r.id)
        # Lane and status first: they hold whoever wrote the 0. Then 117's own
        # 0s go back before the exemption check: 117 never read tax_exemption,
        # so a lapsed or switched-off exemption row did not stop it zeroing a
        # quote that was taxed, and an active one resolves NULL to 0 anyway.
        if r.found_job_id is None or is_install_lane(r.job_type):
            lane_left.append(eid)
        elif r.status not in _OPEN:
            closed_left[eid] = r.status
        elif eid in by_117:
            changed.append(r)
        elif r.has_exemption:
            exempt_left.append(eid)
        elif r.status == "sent":
            sent_left[eid] = r.status
        else:
            changed.append(r)

    now = datetime.now(UTC)
    for r in changed:
        bind.execute(_NULL, {"id": r.id, "now": now})
    for eid in sent_left:
        log.warning("121: estimate %s is sent at an explicit tax_rate 0 117 did not write; left as is", eid)
    for eid, status in closed_left.items():
        log.warning("121: estimate %s is %s at tax_rate 0; left as is", eid, status)
    log.info(
        "121: %d open service-lane trade estimate(s) set to tax_rate NULL; left: %d sent explicit 0, "
        "%d closed, %d with an exemption record, %d job-less or install-lane",
        len(changed), len(sent_left), len(closed_left), len(exempt_left), len(lane_left),
    )
    if not changed:
        return

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    tenants = sorted({r.company_id for r in changed if r.company_id})
    log_audit_event_sync(
        session,
        tenant_id=(tenants[0] if len(tenants) == 1 else None),
        user_id=ACTOR,
        action=ACTION,
        entity_type="estimates",
        entity_id="batch",
        details={
            "from": 0,
            "to": None,
            "reason": (
                "trade customers are taxed at the tenant default unless exempt (Doug, 2026-10-13); "
                "supersedes 117's 'never charge sales tax' on service-lane estimates (GDXA-907)"
            ),
            "estimate_ids": [_dashed(r.id) for r in changed],
            "left_sent_explicit_zero": sent_left,
            "left_closed": closed_left,
            "left_exemption_record": exempt_left,
            "left_jobless_or_install": lane_left,
        },
    )
    session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"estimates", "audit_logs"} <= tables:
        return
    ids = _logged_ids(bind, ACTION)
    if not ids:
        return
    dialect = bind.dialect.name
    result = bind.execute(
        _RESTORE, {"ids": [_stored(dialect, i) for i in ids], "now": datetime.now(UTC)}
    )
    log.info("121 downgrade: %d estimate(s) set back to tax_rate 0", result.rowcount)

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
