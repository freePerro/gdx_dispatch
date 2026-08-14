from __future__ import annotations

import asyncio
import secrets
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import SYSTEM_ACTOR, log_audit_event, utcnow
from gdx_dispatch.modules.proposals.models import Estimate, ProposalTier, ProposalTierLine


def _money2(v) -> Decimal:
    """2dp money quantization — same shape mobile_quoting._money applies to
    est.total at accept, kept local to avoid a modules→routers import."""
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def tier_contract_lines(db: Session, tier: ProposalTier) -> list[ProposalTierLine]:
    """The tier's own line items, in display order. Empty for a flat tier."""
    return list(db.execute(
        select(ProposalTierLine)
        .where(ProposalTierLine.tier_id == tier.id)
        .order_by(ProposalTierLine.sort_order.asc(), ProposalTierLine.id.asc())
    ).scalars().all())


def tier_contract_subtotal(db: Session, tier: ProposalTier) -> Decimal:
    """What accepting this tier commits the customer to: Σ its lines when it
    is line-built, else its manual total_price. This is the ONE number the
    accept flows write into est.total, the invoice bills, and the deposit
    percent applies to — page and invoice agree because they share it."""
    lines = tier_contract_lines(db, tier)
    if lines:
        return _money2(sum(Decimal(str(ln.line_total or 0)) for ln in lines))
    return _money2(tier.total_price)


def next_estimate_number(db: Session) -> str:
    """The next canonical ``EST-NNNNNN`` number — the ONE minter.

    Max numeric suffix of canonical 10-char EST-NNNNNN numbers (+1), across live
    AND soft-deleted rows (the unique constraint covers both). Ignores:
      * legacy formats (``EST-JOB-2026-0018``), and
      * option variants (``EST-000042-1`` — len != 10). A variant must never
        advance the main counter, or the next real estimate would skip numbers.

    Every estimate-number minter delegates here so the three former copies can't
    drift again — one of them (mobile_quoting) did `int(split("-",1)[1])` on the
    most-recent number and produced a RANDOM number the moment a "-N" variant
    existed. ``count(*)+1`` was another old approach; it collides on
    deletes/skips (prod incident 2026-05-07: EST-000027 already taken).
    """
    rows = db.execute(select(Estimate.estimate_number)).scalars().all()
    highest = 0
    for n in rows:
        if not n or len(n) != 10 or not n.startswith("EST-"):
            continue
        suffix = n[4:]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"EST-{highest + 1:06d}"


def create_estimate(job_id: UUID, estimate_number: str, db: Session, company_id: str = "tenant-test") -> Estimate:
    # company_id is NOT NULL (Build Rule 5). Default is only used by unit
    # tests that call this helper directly; production routers pull the real
    # tenant id from request.state.tenant and pass it explicitly.
    row = Estimate(
        job_id=job_id,
        estimate_number=estimate_number,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=company_id,
    )
    db.add(row); db.commit(); db.refresh(row); return row  # noqa: E701,E702


TIER_ORDER = {"good": 0, "better": 1, "best": 2}


def _log_tier_event(db: Session, action: str, actor: str, estimate_id: UUID, details: dict) -> None:
    """Tier writes change the priced offer, so they belong in the audit trail.

    accept_tier has always logged; create/update/delete did not, which meant a
    tier could be re-priced on a live estimate with no record of who or when.
    Mirrors accept_tier's asyncio.run(log_audit_event(...)) call shape.
    """
    asyncio.run(log_audit_event(db, action, actor, "estimate", str(estimate_id), details))


def _editable_estimate(estimate_id: UUID, db: Session) -> Estimate:
    """The estimate, or 404 — and 409 once the customer has settled it.

    Same rule as estimates._ensure_editable. Tiers are the priced offer itself,
    so re-pricing them after an accept would silently rewrite what the customer
    agreed to (and what the invoice was drafted from).
    """
    est = db.execute(select(Estimate).where(Estimate.id == estimate_id)).scalar_one_or_none()
    if not est: raise HTTPException(status_code=404, detail="Estimate not found")  # noqa: E701,E702
    if est.status in {"accepted", "declined"}: raise HTTPException(status_code=409, detail="cannot edit tiers on a finalized estimate")  # noqa: E701,E702
    return est


def _tier_or_404(estimate_id: UUID, tier_id: UUID, db: Session) -> ProposalTier:
    tier = db.execute(select(ProposalTier).where(ProposalTier.id == tier_id, ProposalTier.estimate_id == estimate_id)).scalar_one_or_none()
    if not tier: raise HTTPException(status_code=404, detail="Proposal tier not found")  # noqa: E701,E702
    return tier


def add_proposal_tier(estimate_id: UUID, tier_name: str, description: str | None, total_price: float, warranty_months: int, db: Session, actor: str = SYSTEM_ACTOR) -> ProposalTier:
    """Create the good/better/best tier, or update it if it already exists.

    Upsert, not blind insert. There are exactly three tier names and the picker
    renders one card per name, so a second POST for "better" must mean "I edited
    better", not "show two better cards" on the customer's PDF. There is no DB
    unique on (estimate_id, tier_name) to fall back on.

    (mobile_quoting builds its ProposalTier rows directly rather than through
    this function, so it is NOT the caller this protects — the office editor is.
    mobile's own duplicate-name path is guarded at its tier_name parse instead.)
    """
    _editable_estimate(estimate_id, db)
    row = db.execute(select(ProposalTier).where(ProposalTier.estimate_id == estimate_id, ProposalTier.tier_name == tier_name)).scalar_one_or_none()
    action = "proposal_tier_updated" if row else "proposal_tier_created"
    if row:
        row.description = description; row.warranty_months = warranty_months  # noqa: E702
        # A line-built tier's price is Σ its lines — a manual total_price on
        # the upsert is silently superseded by the sync (the office UI sends
        # the already-synced value; an API caller sending anything else would
        # otherwise make the card show one number and the accept record
        # another).
        if tier_contract_lines(db, row):
            _resync_tier_price(db, row)
        else:
            row.total_price = total_price
    else:
        row = ProposalTier(estimate_id=estimate_id, tier_name=tier_name, description=description, total_price=total_price, warranty_months=warranty_months, display_order=TIER_ORDER.get(tier_name, 0))
        db.add(row)
    _log_tier_event(db, action, actor, estimate_id, {"tier_name": tier_name, "total_price": str(total_price)})
    db.commit(); db.refresh(row); return row  # noqa: E701,E702


def update_proposal_tier(estimate_id: UUID, tier_id: UUID, fields: dict, db: Session, actor: str = SYSTEM_ACTOR) -> ProposalTier:
    """Patch one tier in place. `fields` is already exclude_unset from the router."""
    _editable_estimate(estimate_id, db)
    tier = _tier_or_404(estimate_id, tier_id, db)
    # Renaming into a name that already exists would recreate exactly the
    # duplicate add_proposal_tier's upsert prevents (two "better" cards on the
    # customer's PDF). There is no DB-level unique on (estimate_id, tier_name),
    # so this is the only thing standing in the way.
    new_name = fields.get("tier_name")
    if new_name and new_name != tier.tier_name:
        clash = db.execute(select(ProposalTier.id).where(
            ProposalTier.estimate_id == estimate_id,
            ProposalTier.tier_name == new_name,
            ProposalTier.id != tier_id,
        )).scalar_one_or_none()
        if clash: raise HTTPException(status_code=409, detail=f"a '{new_name}' tier already exists on this estimate")  # noqa: E701,E702
    # A line-built tier's price cannot be hand-set: it IS the sum of its
    # lines, and the accept paths record that sum — honoring a manual PATCH
    # here would let the public card show $9,999 while the contract books
    # $6,000. The field is dropped (not 409d) so the office UI's whole-card
    # save, which echoes the synced value, keeps working.
    if "total_price" in fields and tier_contract_lines(db, tier):
        fields = {k: v for k, v in fields.items() if k != "total_price"}
    for key, value in fields.items():
        setattr(tier, key, value)
    if new_name:
        tier.display_order = TIER_ORDER.get(tier.tier_name, 0)
    _log_tier_event(db, "proposal_tier_updated", actor, estimate_id,
                    {"tier_id": str(tier_id), "tier_name": tier.tier_name, "changed": sorted(fields)})
    db.commit(); db.refresh(tier); return tier  # noqa: E701,E702


def delete_proposal_tier(estimate_id: UUID, tier_id: UUID, db: Session, actor: str = SYSTEM_ACTOR) -> None:
    """Drop a tier.

    Refuses to delete the accepted one: Estimate.accepted_tier_id is an FK and
    the invoice is drafted from the accepted tier's price, so removing it would
    leave a dangling reference and an invoice with no basis. _editable_estimate
    already blocks the common case (an accepted estimate is not editable at
    all); this is the belt-and-braces check for a stale accepted_tier_id left
    on an estimate that was later reopened to draft.
    """
    _editable_estimate(estimate_id, db)
    tier = _tier_or_404(estimate_id, tier_id, db)
    est = db.execute(select(Estimate).where(Estimate.id == estimate_id)).scalar_one_or_none()
    if est is not None and est.accepted_tier_id == tier.id: raise HTTPException(status_code=409, detail="cannot delete the accepted tier")  # noqa: E701,E702
    _log_tier_event(db, "proposal_tier_deleted", actor, estimate_id,
                    {"tier_id": str(tier_id), "tier_name": tier.tier_name, "total_price": str(tier.total_price)})
    db.delete(tier); db.commit()  # noqa: E702


def accept_tier(estimate_id: UUID, tier_id: UUID, db: Session, actor: str = SYSTEM_ACTOR) -> Estimate:
    est = db.execute(select(Estimate).where(Estimate.id == estimate_id)).scalar_one_or_none()
    if not est: raise HTTPException(status_code=404, detail="Estimate not found")  # noqa: E701,E702
    tier = db.execute(select(ProposalTier).where(ProposalTier.id == tier_id, ProposalTier.estimate_id == estimate_id)).scalar_one_or_none()
    if not tier: raise HTTPException(status_code=404, detail="Proposal tier not found")  # noqa: E701,E702
    est.status = "accepted"; est.accepted_at = utcnow(); est.accepted_tier_id = tier_id  # noqa: E701,E702
    # The accepted price must land in the billing data model, not just the
    # pointer (2026-08-14 tier-accept fix; mobile_quoting has done this since
    # its accept shipped). est.total drives compute_estimate_totals, the
    # invoice subtotal and the deposit default — leaving it at the base-lines
    # sum billed $500 jobs on $8,000 accepts.
    est.total = tier_contract_subtotal(db, tier)
    est.updated_at = utcnow()
    asyncio.run(log_audit_event(db, "proposal_tier_accepted", actor, "estimate", str(est.id), {"tier_id": str(tier_id), "tier_name": tier.tier_name, "contract_total": str(est.total)}))
    db.commit(); db.refresh(est); return est  # noqa: E701,E702


# ── tier line items (2026-08-14: tiers built like estimates) ─────────────────


def _resync_tier_price(db: Session, tier: ProposalTier) -> None:
    """A line-built tier's price IS the sum of its lines — one source of
    truth. Called by every tier-line write; a tier with no lines keeps its
    manual total_price (today's flat behavior, fully backward compatible).
    No commit — callers own the transaction."""
    lines = tier_contract_lines(db, tier)
    if lines:
        tier.total_price = _money2(sum(Decimal(str(ln.line_total or 0)) for ln in lines))


def _tier_line_or_404(tier: ProposalTier, line_id: UUID, db: Session) -> ProposalTierLine:
    row = db.execute(select(ProposalTierLine).where(
        ProposalTierLine.id == line_id, ProposalTierLine.tier_id == tier.id
    )).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Tier line not found")  # noqa: E701,E702
    return row


def add_tier_line(
    estimate_id: UUID, tier_id: UUID, *, description: str, quantity: int,
    unit_price: float, category: str | None, db: Session, actor: str = SYSTEM_ACTOR,
) -> ProposalTierLine:
    est = _editable_estimate(estimate_id, db)
    tier = _tier_or_404(estimate_id, tier_id, db)
    qty = max(1, int(quantity or 1))
    unit = _money2(unit_price)
    next_sort = 1 + max([ln.sort_order for ln in tier_contract_lines(db, tier)] or [0])
    row = ProposalTierLine(
        tier_id=tier.id,
        description=description,
        category=category,
        quantity=qty,
        unit_price=unit,
        line_total=_money2(unit * qty),
        sort_order=next_sort,
        company_id=str(est.company_id or ""),
    )
    db.add(row)
    db.flush()
    _resync_tier_price(db, tier)
    _log_tier_event(db, "proposal_tier_line_created", actor, estimate_id,
                    {"tier_id": str(tier_id), "tier_name": tier.tier_name, "description": description[:120], "line_total": str(row.line_total)})
    db.commit(); db.refresh(row); return row  # noqa: E701,E702


def update_tier_line(
    estimate_id: UUID, tier_id: UUID, line_id: UUID, fields: dict, db: Session, actor: str = SYSTEM_ACTOR,
) -> ProposalTierLine:
    _editable_estimate(estimate_id, db)
    tier = _tier_or_404(estimate_id, tier_id, db)
    row = _tier_line_or_404(tier, line_id, db)
    for key in ("description", "category"):
        if key in fields:
            setattr(row, key, fields[key])
    if "quantity" in fields:
        row.quantity = max(1, int(fields["quantity"] or 1))
    if "unit_price" in fields:
        row.unit_price = _money2(fields["unit_price"])
    row.line_total = _money2(Decimal(str(row.unit_price)) * row.quantity)
    _resync_tier_price(db, tier)
    _log_tier_event(db, "proposal_tier_line_updated", actor, estimate_id,
                    {"tier_id": str(tier_id), "line_id": str(line_id), "changed": sorted(fields)})
    db.commit(); db.refresh(row); return row  # noqa: E701,E702


def delete_tier_line(
    estimate_id: UUID, tier_id: UUID, line_id: UUID, db: Session, actor: str = SYSTEM_ACTOR,
) -> None:
    _editable_estimate(estimate_id, db)
    tier = _tier_or_404(estimate_id, tier_id, db)
    row = _tier_line_or_404(tier, line_id, db)
    db.delete(row)
    db.flush()
    _resync_tier_price(db, tier)
    _log_tier_event(db, "proposal_tier_line_deleted", actor, estimate_id,
                    {"tier_id": str(tier_id), "line_id": str(line_id), "description": (row.description or "")[:120]})
    db.commit()
