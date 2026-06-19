"""Canonical job display-state derivation — the single source of truth.

The "flow" a customer engagement goes through spans three tenant-plane
tables — Estimate -> Job -> Invoice — but ~49 Vue surfaces and ~16 routers
each independently string-render `Job.status`/`lifecycle_stage` and stop at
"Complete", which is deceiving: a job still has to be billed and paid, and
`Job.billing_status` is a broken cache that never advances past `invoiced`.

This module derives ONE typed state for the whole flow so every surface
shows the same answer. Model locked with Doug 2026-05-17, web-validated
against QuickBooks / Stripe / Salesforce / Jobber:

  OPEN  (type=open) : Lead -> Service Call -> Estimate -> Scheduled ->
                      In Progress -> Ready to Bill -> Invoiced ->
                      Partially Paid   [Overdue = flag on Invoiced]
  FINISHED (typed terminal, flow stops, mirrors QB):
      Paid        won  <- QB Invoice "Paid"
      Declined    lost <- QB Estimate "Rejected"   (Estimate.status)
      Cancelled   lost <- QB Invoice "Voided"      (Job.lifecycle_stage)
      Written Off lost <- QB bad-debt credit memo  (NET-NEW, Slice 2)

This is a PURE function: it takes primitives, never touches the DB (callers
pass the already-loaded estimate status + invoice summaries — no N+1, no
plane-crossing imports, trivially unit-testable over real prod
permutations). It supersedes the work-axis-only `_canon_status()` in
`gdx_dispatch/routers/jobs.py`.

Slice 1 (this file) is zero-migration: Written Off is coded but
unreachable until Slice 2 adds the `invoice_status` enum value; everything
else derives from existing fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

logger = logging.getLogger(__name__)

# Terminal type tags (Salesforce stage-Type pattern: ask the *type*
# structurally, never string-match a label).
TYPE_OPEN = "open"
TYPE_WON = "won"
TYPE_LOST = "lost"

# Estimate.status values that mean "the customer said no to the quote".
_ESTIMATE_LOST = {"declined", "rejected", "expired"}
# Invoice.status values that mean "billed, awaiting / settled money".
# `written_off`/`uncollectible` is NET-NEW (Slice 2) — listed so the branch
# exists today and Slice 2 only has to add the stored value.
_INVOICE_WRITTEN_OFF = {"written_off", "uncollectible", "bad_debt"}
_INVOICE_VOID = {"void", "voided"}

# Open work-axis labels, keyed by Job.lifecycle_stage. Kept in lockstep
# with the legacy `_STATUS_CANON` in jobs.py (this supersedes it).
_LIFECYCLE_LABEL = {
    "lead": "Lead",
    "service_call": "Service Call",
    "estimate": "Estimate",
    "scheduled": "Scheduled",
    "in_progress": "In Progress",
    "completed": "Complete",  # transitional only — money axis overrides
    "cancelled": "Cancelled",
}


@dataclass(frozen=True)
class DisplayState:
    """One canonical state for the whole Estimate->Job->Invoice flow."""

    stage: str  # machine key, e.g. "paid", "invoiced", "service_call"
    type: str  # TYPE_OPEN | TYPE_WON | TYPE_LOST
    label: str  # human label, e.g. "Paid", "Ready to Bill"

    @property
    def is_finished(self) -> bool:
        """A flow is finished iff it reached a typed terminal."""
        return self.type in (TYPE_WON, TYPE_LOST)

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "type": self.type,
            "label": self.label,
            "is_finished": self.is_finished,
        }


def _num(v: object) -> Decimal:
    """Coerce balance/amount fields (Decimal | float | str | None) safely."""
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (ValueError, ArithmeticError):
        return Decimal("0")


def derive_job_display_state(
    *,
    lifecycle_stage: str | None,
    estimate_status: str | None = None,
    invoices: Iterable[dict] | None = None,
) -> DisplayState:
    """Derive the one canonical display state for a job's whole flow.

    Args:
        lifecycle_stage: ``Job.lifecycle_stage`` (work axis).
        estimate_status: ``Estimate.status`` of the originating estimate,
            if the job came from one (``None`` if no estimate).
        invoices: iterable of ``{"status", "balance_due", "amount_paid"}``
            dicts for invoices linked to the job (``None``/empty if none).

    Precedence: terminals first; the money axis overrides the work axis
    once work is complete; never silently returns "Unknown".
    """
    lc = (lifecycle_stage or "").strip().lower()
    est = (estimate_status or "").strip().lower()
    inv_list = [i for i in (invoices or []) if i]

    # --- 1. Cancelled (lost) — work was called off, beats everything. ---
    if lc == "cancelled":
        return DisplayState("cancelled", TYPE_LOST, "Cancelled")

    # Non-void invoices are the ones that carry money meaning.
    live_invoices = [
        i for i in inv_list
        if str(i.get("status", "")).strip().lower() not in _INVOICE_VOID
    ]

    # --- 2. Declined (lost) — customer rejected the quote. Only valid ---
    # when the flow never became real work/money (no live invoice and
    # still on the quote side of the pipeline).
    if (
        est in _ESTIMATE_LOST
        and not live_invoices
        and lc in ("", "lead", "service_call", "estimate")
    ):
        return DisplayState("declined", TYPE_LOST, "Declined")

    # --- 3. Written Off (lost) — bad debt. NET-NEW: unreachable until ---
    # Slice 2 adds the invoice_status value; the branch exists so Slice 2
    # is storage-only.
    if any(
        str(i.get("status", "")).strip().lower() in _INVOICE_WRITTEN_OFF
        for i in inv_list
    ):
        return DisplayState("written_off", TYPE_LOST, "Written Off")

    if live_invoices:
        statuses = [str(i.get("status", "")).strip().lower() for i in live_invoices]

        # --- 4. Paid (won) — every live invoice settled. ---
        all_paid = all(
            s == "paid" or _num(i.get("balance_due")) <= 0
            for s, i in zip(statuses, live_invoices)
        )
        if all_paid:
            return DisplayState("paid", TYPE_WON, "Paid")

        # --- 5. Money-axis open states (work done, money pending). ---
        if any(s == "overdue" for s in statuses):
            return DisplayState("overdue", TYPE_OPEN, "Overdue")
        if any(
            _num(i.get("amount_paid")) > 0 and _num(i.get("balance_due")) > 0
            for i in live_invoices
        ):
            return DisplayState("partially_paid", TYPE_OPEN, "Partially Paid")
        # Anything else with a live invoice = billed, awaiting payment.
        return DisplayState("invoiced", TYPE_OPEN, "Invoiced")

    # No live invoice. Work physically done but not yet billed.
    if lc == "completed":
        return DisplayState("ready_to_bill", TYPE_OPEN, "Ready to Bill")

    # --- 6. Work-axis open states. ---
    if lc in _LIFECYCLE_LABEL:
        return DisplayState(lc, TYPE_OPEN, _LIFECYCLE_LABEL[lc])

    # --- 7. Never silent. Title-case the input, log the surprise. ---
    fallback = (lifecycle_stage or "").strip()
    if fallback:
        logger.warning(
            "derive_job_display_state: unmapped lifecycle_stage=%r "
            "(estimate_status=%r, invoices=%d) — falling back to titled",
            lifecycle_stage, estimate_status, len(inv_list),
        )
        return DisplayState(fallback.lower(), TYPE_OPEN, fallback.title())
    return DisplayState("unknown", TYPE_OPEN, "Unknown")
