"""Closeout → draft invoice: the ONE line-builder, and the autodraft hook.

Two callers, one pricing implementation (the A1 rule — never fork a second
implementation of an ownership/pricing gate):

* ``routers/mobile_invoicing.py`` — the tech taps "Create invoice" on the
  truck; lines are built into the invoice that endpoint minted.
* ``routers/jobs.py::closeout_job`` — the autodraft (Doug 2026-08-07):
  closing out a job with no accepted estimate creates a DRAFT invoice
  right away, so Ready-for-Billing reviews an existing priced draft
  instead of starting from a blank form.

Pricing itself lives in ``core/billing_lanes`` (service/install lanes) and
is unchanged here. UNPRICED closeout parts are deliberately never lined:
they stay on the office checklist, and the §11 verification gate holds the
invoice until a human prices them — nothing is silently $0-lined onto a
customer PDF.

The autodraft is REBUILDABLE while untouched: a re-closeout wipes and
re-lines the same draft (same invoice number — "the invoice for this job"
stays one thing to look at), and Not-billable voids it. "Untouched" is
strict — machine origin AND draft status AND never verified/sent/locked/
paid. The moment a human advances the draft, the machine keeps its hands
off and the §12 discrepancy flow takes over.
"""
from __future__ import annotations

import logging
import secrets
import uuid as _uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import contained_read
from gdx_dispatch.core.pay_periods import shop_today_from_settings
from gdx_dispatch.core.quantities import recorded_quantity, zero_quantity_verdict
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobCloseout, JobPartNeeded
from gdx_dispatch.modules.proposals.models import Estimate

log = logging.getLogger(__name__)

AUTODRAFT_ORIGIN = "closeout_autodraft"
# `invoice_lines.source` for a line THIS builder wrote (migration 075). Any
# other value — including NULL — is treated as possibly-human and stops the
# machine from deleting the line. NULL is the honest reading for every line
# that predates the column: what nobody recorded cannot now be inferred, and
# deleting an operator's work on a guess is the worse of the two errors.
AUTODRAFT_LINE_SOURCE = "autodraft"


def _money(v: Decimal | float | str) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Multi-day jobs: the day rows (plan §5.4a, migration 106)
# ---------------------------------------------------------------------------
# A day row is one person's attested hours for one shop day, written by
# day-close ("No" on the closeout sheet): `day_closed_at` set, minutes > 0,
# not deleted, closed. A timer day-close consumed at 0 minutes carries the
# marker but is not a day row, and adds nothing anywhere below.
#
# Two different sums are read from them, and they must not be confused:
#   * MAN-hours (billing): every row, every person, every day. A crew of two
#     working 8 h each is 16 man-hours of labor the customer pays for.
#   * WALL-CLOCK hours (D13 queued hours, tech efficiency): per shop day, the
#     LONGEST row. The same crew used 8 h of the job's schedule, not 16.

EARLIER_VISITS_DESCRIPTION = "Labor — earlier visits"


def _job_uuid(value) -> _uuid.UUID | None:
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def day_row_entries(db: Session, job_ids) -> dict[str, list[tuple[datetime, int]]]:
    """``{str(job_id): [(clock_in, duration_minutes), ...]}`` for the jobs'
    day rows. Jobs with none are absent. One query for any number of jobs."""
    from gdx_dispatch.models.tenant_models import TimeEntry  # noqa: PLC0415

    ids = [u for u in (_job_uuid(j) for j in job_ids) if u is not None]
    if not ids:
        return {}
    rows = db.execute(
        select(TimeEntry.job_id, TimeEntry.clock_in, TimeEntry.duration_minutes).where(
            TimeEntry.job_id.in_(ids),
            TimeEntry.day_closed_at.is_not(None),
            TimeEntry.duration_minutes > 0,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.clock_out.is_not(None),
        )
    ).all()
    out: dict[str, list[tuple[datetime, int]]] = {}
    for job_id, clock_in, minutes in rows:
        out.setdefault(str(_job_uuid(job_id)), []).append((clock_in, int(minutes)))
    return out


def longest_day_row_minutes(entries, tz_name: str) -> dict:
    """``{shop-local date: longest row's minutes}``: one job's wall-clock
    time per shop day. A row's day is its ``clock_in``'s shop-local day."""
    from gdx_dispatch.core.pay_periods import shop_day_of  # noqa: PLC0415

    per_day: dict = {}
    for clock_in, minutes in entries:
        day = shop_day_of(clock_in, tz_name)
        if day is None:
            continue
        per_day[day] = max(per_day.get(day, 0), int(minutes))
    return per_day


def worked_wall_clock_hours(db: Session, job_ids) -> dict[str, float]:
    """``{str(job_id): Σ per shop day of the longest day row, in hours}``
    (D13). Jobs with no day rows are absent; read a miss as 0."""
    from gdx_dispatch.core.pay_periods import shop_tz_name_from_settings  # noqa: PLC0415

    entries = day_row_entries(db, job_ids)
    if not entries:
        return {}
    tz_name = shop_tz_name_from_settings(db)
    return {
        jid: sum(longest_day_row_minutes(rows, tz_name).values()) / 60
        for jid, rows in entries.items()
    }


def earlier_visits_line(db: Session, job, *, job_type: str | None = None) -> dict | None:
    """The "Labor — earlier visits" line (plan §5.4a Billing, Doug's R-P1),
    or None when the job has no day-row hours or is not a service-lane job.

    ``job`` is a ``Job`` or a job id; with an id, pass ``job_type``.

    R-P1, money: the day rows' MAN-hours are summed across days and people,
    rounded UP to the half hour ONCE, and billed at the plain hourly rate.
    No first-hour price and no 1-hour floor: the first-hour price is charged
    once, on the final day's line. That is why this does not reuse
    ``service_labor_line`` / ``billed_man_hours`` — reusing them would charge
    a three-day repair the first-hour price twice.

    The returned dict has the shape of the closeout-billing-suggestion's
    ``labor_line``. ``source`` there is LABOR provenance, and this is
    ``"attested"``: day rows are hours a person typed at day-close, the same
    evidence class as ``closeout.hours_worked``. ``build_closeout_lines``
    stamps the INVOICE line's ``source`` column ``AUTODRAFT_LINE_SOURCE``.

    ``quantity`` is 1 and ``unit_price`` == ``line_total``, with the math in
    the description, exactly like the service-lane labor line:
    ``invoice_lines.quantity`` is an INTEGER column, and a 7.5 written there
    rounds to 8 on Postgres while ``line_total`` still says 7.5 h — a
    recalculation from quantity × price would then over-bill.

    The caller owns WHEN it applies (the suggestion endpoint offers it only on
    a completed job). Accepted-estimate jobs never reach the autodraft.
    """
    from gdx_dispatch.core.billing_lanes import lane_for_job, service_rates  # noqa: PLC0415

    job_id = getattr(job, "id", job)
    if job_type is None:
        job_type = getattr(job, "job_type", None)
    if lane_for_job(job_type) != "service":
        return None
    jid = _job_uuid(job_id)
    if jid is None:
        return None
    total_minutes = sum(m for _c, m in day_row_entries(db, [jid]).get(str(jid), []))
    if total_minutes <= 0:
        return None
    # Rounded up to the half hour once, in exact integer arithmetic: a float
    # 7.2 * 2 is not guaranteed to ceil to 15.
    billed_half_hours = -(-total_minutes // 30)
    billed_hours = Decimal(billed_half_hours) / 2
    _first, hourly = service_rates(db)
    amount = _money(hourly * billed_hours)
    description = (
        f"{EARLIER_VISITS_DESCRIPTION} — {billed_hours:.2f} man-hours"
        f" at ${hourly}/hr"
    )
    return {
        "description": description[:500],
        "quantity": 1,
        "unit_price": float(amount),
        "line_total": float(amount),
        "source": "attested",
        "labor_price_item_id": None,
        "man_hours": round(total_minutes / 60, 2),
    }


def next_invoice_number(db: Session) -> str:
    """The ONE invoice-number generator (2026-08-08 audit: there were FOUR —
    this max-based one, a count-based one in routers/invoices that re-issued
    taken numbers whenever count and max diverged, and two hex schemes on
    dead endpoints). All live creation paths delegate here now.

    Highest sequential number + 1, then bump past any takers — so a
    hex-format historical row, a deleted row, or a same-instant sibling
    can't produce a duplicate. The unique constraint on invoice_number
    remains the final referee; callers on hot paths retry once on
    IntegrityError for the residual race.
    """
    # Fixed-width zero-padded numbers sort lexicographically, so MAX() over
    # the LIKE-shaped set finds the true high-water mark even when hex-form
    # or imported numbers exist alongside (the old latest-by-created_at read
    # fell to the hex fallback the moment the newest row wasn't sequential).
    row = db.execute(
        _text(
            "SELECT MAX(invoice_number) FROM invoices "
            "WHERE invoice_number LIKE 'INV-______'"
        )
    ).first()
    n = 0
    if row and row[0]:
        try:
            n = int(str(row[0]).split("-", 1)[1])
        except (ValueError, AttributeError):
            n = 0
    for candidate_n in range(n + 1, n + 51):
        candidate = f"INV-{candidate_n:06d}"
        taken = db.execute(
            _text("SELECT 1 FROM invoices WHERE invoice_number = :c LIMIT 1"),
            {"c": candidate},
        ).first()
        if taken is None:
            return candidate
    return f"INV-{datetime.now(UTC):%y%m}{secrets.token_hex(2).upper()}"


def flush_invoice_with_number_retry(db: Session, invoice, *, already_won=None):
    """Flush a freshly-built invoice; absorb an invoice_number collision.

    Two same-instant creates can compute the same number — the bump-past-takers
    generator cannot see an uncommitted sibling, so the unique constraint is
    the referee. On that specific IntegrityError: roll back, regenerate, flush
    again. Any other IntegrityError re-raises untouched.

    ``already_won`` (M17.4, adversarial review): **db.rollback() releases row
    locks.** A caller that serialized concurrent requests with SELECT…FOR
    UPDATE (the deposit path) lets its rival through the moment we roll back —
    and the rival may legitimately create the very invoice we were building.
    The callable re-checks after the rollback; a non-None result is the
    winner's row, returned for the caller to adopt INSTEAD of double-minting.
    Callers that adopt must not keep building on their own dead row — compare
    identity and return early.

    Extracted from three near-identical inline blocks (office, mobile,
    deposit) so the behavior is tested once, behaviorally, instead of pinned
    by three source-text presence tests.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        db.flush()
        return invoice
    except IntegrityError as exc:
        if "invoice_number" not in str(exc):
            raise
        db.rollback()
        if already_won is not None:
            winner = already_won()
            if winner is not None:
                return winner
        db.add(invoice)
        invoice.invoice_number = next_invoice_number(db)
        db.flush()
        return invoice


def build_closeout_lines(
    db: Session,
    *,
    tenant_id: str,
    invoice: Invoice,
    closeout: JobCloseout,
    job_type: str | None,
    job_id: str,
) -> tuple[int, Decimal, Decimal]:
    """Add invoice lines priced from the closeout. Returns (lines_added,
    lines_total, taxable_total). Extracted from the mobile truck path (§8).

    Taxable flags (2026-08-08 audit): labor lines carry the tenant's
    tax-labor flag (same M24 rule the estimate→invoice copy applies — the
    old default-True meant a later office line-edit recalc would suddenly
    tax labor); parts stay taxable. taxable_total feeds the caller's tax
    computation so an autodraft with a resolved rate taxes parts exactly
    like an office-created invoice would.

    Lanes decide (core/billing_lanes): service → hourly labor line; install
    with a picked matrix row → flat price; everything else → labor stays
    UNPRICED and the §11 verification gate is the flag.

    Parts: the closeout's attested, still-unbilled checklist rows. PRICED
    rows become lines and are claimed with the same stamp-first rule as the
    office path (a row the stamp cannot claim was billed by a concurrent
    invoice — skip it, never double-bill). UNPRICED rows are deliberately
    left unstamped: they stay on the office checklist.
    """
    from gdx_dispatch.core.billing_lanes import (
        _as_uuid,
        install_labor_line,
        lane_for_job,
        service_labor_line,
    )

    # The closeout's JobPartNeeded rows may be pending in this session
    # (closeout_job adds them in the same transaction); the SELECT below
    # must see them even with autoflush off.
    db.flush()

    # NOT `contained_read`-wrapped, on purpose — GDXA-160 counted this site and
    # deferred it rather than reaching across an ownership line with the wrong
    # tool. `_load_tax_labor_flag` swallows its OWN DB failure
    # (`except Exception: return False`), so on an aborted Postgres transaction
    # it returns cleanly, the `with` block would exit clean, and the CM's
    # RELEASE SAVEPOINT raises 25P02 out of the `with` itself while the caller's
    # commit stays just as dead — `contained_read` rule 5, strictly worse than
    # leaving it alone. The containment has to go inside the helper, and
    # `modules/proposals/totals.py` is estimates-pricing's file, not this one.
    # Rule 5 names a second cure too — a variant of `contained_read` that always
    # ROLLBACK-TO-SAVEPOINTs instead of RELEASEing, measured to work on PG 15.17
    # and 16.14 — so this is a deferral with two live cures, not "unfixable".
    # Until then the failure mode is real: the flag reads False, labor is billed
    # untaxed (which is the correct answer for MN garage-door work anyway), and
    # the caller's transaction is dead.
    #
    # WHICH caller matters, and this builder has two. `closeout_job` calls it
    # inside `db.begin_nested()`, so the damage is bounded to the autodraft.
    # `routers/mobile_invoicing.py::mobile_create_invoice` does NOT: the tech's
    # Invoice is `db.add`ed and flushed long before this line and is not
    # committed until the end of that handler, with no savepoint anywhere on
    # the frame. That is the path where this deferral costs a real invoice, and
    # it is the reason the deferral is worth fixing rather than living with.
    try:
        from gdx_dispatch.modules.proposals.totals import _load_tax_labor_flag  # noqa: PLC0415
        labor_taxable = bool(_load_tax_labor_flag(db))
    except Exception:  # noqa: BLE001 — flag read must never block billing
        labor_taxable = False

    sort = 1
    lines_added = 0
    lines_total = Decimal("0")
    taxable_total = Decimal("0")
    lane = lane_for_job(job_type)
    if lane == "install" and getattr(closeout, "labor_matrix_item_id", None):
        # Install lane: flat price from the picked matrix row. If the row is
        # gone/inactive/$0, _install is None → labor stays unpriced (office
        # lane), never guessed.
        _install = install_labor_line(db, closeout.labor_matrix_item_id)
        if _install is not None:
            db.add(InvoiceLine(
                id=_uuid.uuid4(),
                invoice_id=invoice.id,
                description=_install.description,
                quantity=_install.quantity,
                unit_price=_install.unit_price,
                line_total=_install.line_total,
                taxable=labor_taxable,
                category="Labor",
                # Migration 071 provenance. This is the DOMINANT path -- prod
                # had 29 labor lines with a NULL source against 1 'matrix',
                # because only the hand-add picker set it. A column that
                # answers "how was this priced?" for 3% of rows answers
                # nothing.
                #
                # A matrix row is a QUOTED FLAT PRICE, so no hours claim rides
                # along: `assumed_man_hours` is the matrix's assumption about a
                # job of that shape, not a record of this one.
                # COERCE. `matrix_item_id` is a str (it comes from
                # `job_closeouts.labor_matrix_item_id`, a varchar(36)) but
                # `invoice_lines.labor_price_item_id` is a UUID column. Postgres
                # casts the string silently; SQLite's UUID adapter calls .hex on
                # it and raises. Caught by CI shard 4, not by local Postgres.
                #
                # Safe unconditionally: `install_labor_line` only returns after
                # loading the row, and sets `matrix_item_id=str(item.id)`, so
                # the value is always a real UUID's string form.
                labor_price_item_id=_as_uuid(_install.matrix_item_id),
                labor_source="matrix",
                # Migration 075 — machine-authored. The INSTALL lane, and the
                # one an adversarial review caught unstamped: this builder
                # writes THREE kinds of line, not two, and missing this one
                # meant the machine built an install autodraft and lost
                # ownership of it in the same transaction — a re-closeout then
                # silently no-opped and "not billable" 409'd on a verb that
                # had worked. If you add a fourth line kind here, stamp it.
                source=AUTODRAFT_LINE_SOURCE,
                sort_order=sort,
                company_id=str(tenant_id),
            ))
            lines_total += _install.line_total
            if labor_taxable:
                taxable_total += _install.line_total
            lines_added += 1
            sort += 1
    if lane == "service" and float(closeout.hours_worked or 0) > 0:
        labor = service_labor_line(
            db,
            hours_worked=float(closeout.hours_worked or 0),
            techs_on_site=int(getattr(closeout, "techs_on_site", 1) or 1),
        )
        db.add(InvoiceLine(
            id=_uuid.uuid4(),
            invoice_id=invoice.id,
            description=labor.description,
            quantity=labor.quantity,
            unit_price=labor.unit_price,
            line_total=labor.line_total,
            taxable=labor_taxable,
            category="Labor",
            # Attested hours are EVIDENCE -- the tech signed them off -- so
            # this lane is the one allowed to record an hours figure. No matrix
            # row: nothing quoted this.
            estimated_man_hours=Decimal(str(labor.attested_hours)),
            labor_source="attested",
            # Migration 075 — machine-authored. `release_untouched_autodraft`
            # deletes every line on an untouched draft so it can rebuild; this
            # stamp is what lets it leave a human's line alone.
            source=AUTODRAFT_LINE_SOURCE,
            sort_order=sort,
            company_id=str(tenant_id),
        ))
        lines_total += labor.line_total
        if labor_taxable:
            taxable_total += labor.line_total
        lines_added += 1
        sort += 1
    # Multi-day jobs (plan §5.4a, R-P1): the days closed by "No" before this
    # closeout. Built OUTSIDE the final-day line's `hours_worked > 0` check on
    # purpose — a job whose final day attests 0 h still bills its earlier
    # days. A job with no day rows gets None here and is unchanged.
    earlier = earlier_visits_line(db, job_id, job_type=job_type) if lane == "service" else None
    if earlier is not None:
        amount = _money(earlier["line_total"])
        from gdx_dispatch.core.pricing_provenance import build_invoice_line  # noqa: PLC0415

        # A new write path, so it records its lane (the line-provenance guard
        # pins the older bare InvoiceLine() constructions in this file).
        db.add(build_invoice_line(
            pricing_source="labor_attested",
            id=_uuid.uuid4(),
            invoice_id=invoice.id,
            description=earlier["description"],
            # Always 1: the math lives in the description (the column is an
            # INTEGER; see earlier_visits_line).
            quantity=1,
            unit_price=amount,
            line_total=amount,
            taxable=labor_taxable,
            category="Labor",
            # The day rows' raw attested man-hours, as the final-day line
            # records its attested hours (not the rounded billed figure).
            estimated_man_hours=Decimal(str(earlier["man_hours"])),
            labor_source="attested",
            # Machine-authored, so a re-closeout's release_untouched_autodraft
            # owns and rebuilds it. The fourth line kind; stamped.
            source=AUTODRAFT_LINE_SOURCE,
            sort_order=sort,
            company_id=str(tenant_id),
        ))
        lines_total += amount
        if labor_taxable:
            taxable_total += amount
        lines_added += 1
        sort += 1

    # Every UNBILLED priced part on the job, not just the closeout-attested
    # ones (2026-08-13). Parts can now be logged as they are installed
    # (source='mobile'/'van'), and the closeout's require-parts gate accepts
    # those rows as evidence the job used parts — so a tech who logs three
    # springs as they go and then closes out was getting an invoice with the
    # labor and NONE of the parts, silently: they are not source='closeout',
    # so this builder skipped them and the unpriced-parts warning counted zero.
    # If a row is good enough to satisfy the completion gate it is good enough
    # to bill, and the claim-then-copy below is still the double-bill guard.
    #
    # ``wont_bill`` is excluded (2026-08-19): it is the office's dismiss verb
    # for warranty / goodwill / already-flat-priced parts
    # (``routers/parts_needed.py`` PartStatusUpdate). Billing a part the office
    # explicitly declined is the one direction this builder must never take,
    # and the query had no status filter at all — a priced ``mobile`` row
    # dismissed before the tech closed out was billed anyway.
    candidate_rows = db.execute(
        select(JobPartNeeded).where(
            JobPartNeeded.job_id == str(job_id),
            JobPartNeeded.source.in_(("closeout", "mobile", "van")),
            JobPartNeeded.billed_invoice_id.is_(None),
            JobPartNeeded.unit_price.is_not(None),
            JobPartNeeded.unit_price > 0,
            or_(
                JobPartNeeded.status.is_(None),
                JobPartNeeded.status != "wont_bill",
            ),
        )
    ).scalars().all()
    for part_row in candidate_rows:
        # A recorded zero is not billed as one (#560, owner 2026-09-11).
        # JobPartNeeded stores no line total — the amount below is unit × qty,
        # so a zero quantity is a $0 line with nothing to bill. Skipped BEFORE
        # the claim, so the row stays unclaimed and on the office checklist
        # for a human to price.
        if zero_quantity_verdict(part_row.quantity, 0) == "skip":
            continue
        claimed = db.execute(
            update(JobPartNeeded)
            .where(
                JobPartNeeded.id == part_row.id,
                JobPartNeeded.billed_invoice_id.is_(None),
            )
            .values(billed_invoice_id=invoice.id)
        ).rowcount
        if not claimed:
            continue
        qty = int(recorded_quantity(part_row.quantity))
        unit = _money(part_row.unit_price or 0)
        db.add(InvoiceLine(
            id=_uuid.uuid4(),
            invoice_id=invoice.id,
            description=(part_row.part_name or part_row.sku or "Part")[:500],
            quantity=qty,
            unit_price=unit,
            line_total=_money(float(unit) * qty),
            taxable=True,
            category="Parts",
            # Stamp the source row (2026-08-19). The office pull has always set
            # this so deleting a line releases the part (D-S122-line-removal-
            # unbill); the autodraft never did, so its lines claimed parts that
            # deleting the line could not give back.
            part_id=part_row.id,
            # Migration 075 — machine-authored, same reason as the labor line.
            source=AUTODRAFT_LINE_SOURCE,
            sort_order=sort,
            company_id=str(tenant_id),
        ))
        lines_total += _money(float(unit) * qty)
        taxable_total += _money(float(unit) * qty)
        lines_added += 1
        sort += 1
    return lines_added, lines_total, taxable_total


def is_untouched_autodraft(inv: Invoice, db: Session | None = None) -> bool:
    """True only while the machine may still rebuild or void this invoice.

    Strict on purpose: any human advancement (verify, send, lock, payment,
    status past draft) permanently ends machine ownership.

    M35: the payment arm used to read ``inv.amount_paid``, a cache nothing
    maintains — it was 0 on every invoice paid since 2026-07-31, so the term
    contributed nothing and this guard leaned entirely on ``paid_at``/status.
    Pass ``db`` to check the payments table for real; without it the payment
    arm is skipped rather than silently answered with a stale 0.
    """
    if not (
        (inv.origin or "") == AUTODRAFT_ORIGIN
        and (inv.status or "draft") == "draft"
        and inv.verified_at is None
        and inv.sent_at is None
        and not bool(inv.locked)
        and inv.paid_at is None
    ):
        return False
    if db is not None:
        from gdx_dispatch.core.invoice_paid import paid_to_date

        if paid_to_date(db, inv.id) != 0:
            return False
        # Follow-up 3 (migration 075). Every arm above asks about the INVOICE;
        # none asked about its LINES, so an invoice the office had added a
        # part to was still "untouched" and `release_untouched_autodraft`
        # deleted the lot — including the line the unbilled-parts banner had
        # just told them to add.
        #
        # `IS DISTINCT FROM` rather than `!=`: in SQL `NULL != 'autodraft'` is
        # NULL, not true, so a plain inequality would silently match nothing
        # and every pre-075 line would read as machine-authored — the exact
        # bug this is here to fix, reintroduced by an operator precedence.
        # Soft-deleted lines are excluded: a line the office already removed
        # is not work to protect.
        human_line = db.execute(
            select(InvoiceLine.id).where(
                InvoiceLine.invoice_id == inv.id,
                InvoiceLine.deleted_at.is_(None),
                InvoiceLine.source.is_distinct_from(AUTODRAFT_LINE_SOURCE),
            ).limit(1)
        ).first()
        return human_line is None
    return True


def _live_autodraft(db: Session, job_id) -> Invoice | None:
    return db.execute(
        select(Invoice).where(
            Invoice.job_id == job_id,
            Invoice.deleted_at.is_(None),
            Invoice.origin == AUTODRAFT_ORIGIN,
            Invoice.status == "draft",
        )
        .order_by(Invoice.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def release_untouched_autodraft(db: Session, *, job: Job) -> Invoice | None:
    """Pre-restatement reset. MUST run BEFORE closeout_job's replace step:
    un-claiming the draft's part stamps turns those rows back into unbilled
    closeout rows, so the replace step deletes them (reversing stock) and the
    new attestation lands cleanly — otherwise the old claimed rows survive as
    "billed" and the re-attested lines get suppressed as billed dupes.

    Returns the emptied draft for ``autodraft_invoice_for_closeout`` to
    rebuild into (same invoice number), or None when there is nothing the
    machine may touch."""
    inv = _live_autodraft(db, job.id)
    if inv is None or not is_untouched_autodraft(inv, db):
        return None
    db.execute(
        update(JobPartNeeded)
        .where(JobPartNeeded.billed_invoice_id == inv.id)
        .values(billed_invoice_id=None)
    )
    # ORM deletes, not a raw Core table-delete: invoice_lines is a money
    # table and raw Core writes are invisible to the ledger flush guard
    # (test_no_raw_core_writes_to_money_tables pins this).
    for line in db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
    ).scalars().all():
        db.delete(line)
    db.flush()
    inv.subtotal = _money(0)
    inv.tax_amount = _money(0)
    inv.total = _money(0)
    inv.balance_due = _money(0)
    return inv


def void_untouched_autodraft(db: Session, inv: Invoice, *, actor: str | None) -> int:
    """Void an untouched autodraft and release its part claims (Not-billable
    path). Caller has already checked ``is_untouched_autodraft``.

    The status goes through the ledger chokepoint like every other void
    (#696). This used to assign the status attribute directly, and with ledger
    posting on, prod's flush guard logged ``gl_chokepoint_bypass`` whenever a
    not-billable mark voided an autodraft. An untouched autodraft is a draft,
    so nothing is posted and no rule runs — what changes is that the write is
    sanctioned, and the guard stays a signal worth reading.

    Returns how many part claims were released, for the caller's audit row.
    """
    from gdx_dispatch.modules.ledger.service import transition_invoice_status

    released = db.execute(
        update(JobPartNeeded)
        .where(JobPartNeeded.billed_invoice_id == inv.id)
        .values(billed_invoice_id=None)
    ).rowcount
    transition_invoice_status(db, inv, "void", actor=actor)
    inv.balance_due = _money(0)
    return int(released or 0)


def autodraft_invoice_for_closeout(
    db: Session,
    *,
    tenant_id: str,
    job: Job,
    closeout: JobCloseout,
    reuse_invoice: Invoice | None = None,
) -> Invoice | None:
    """Create (or rebuild) the draft invoice for a just-submitted closeout.

    Runs inside closeout_job's transaction, AFTER the closeout snapshot and
    its parts rows are in the session. Eligibility — every skip returns
    None and the closeout proceeds unbilled (Ready-for-Billing still shows
    the job with the classic Create Invoice action):

    * office marked the job not billable → never bill it by machine
    * no customer → invoices.customer_id is NOT NULL
    * an accepted estimate exists → §15.1: the estimate outranks the lanes;
      the office/mobile estimate paths own that invoice
    * any live non-void invoice already exists (and we're not rebuilding) →
      one invoice story per job; deposits/partials mean a human is driving

    A NEW draft is only kept when at least one priced line landed — an empty
    $0 draft on every unpriceable closeout would just be queue noise. A
    REBUILT draft is kept even at zero lines: it already exists, and leaving
    it live keeps its number stable for the office to fill in.
    """
    if job.not_billable_at is not None:
        return None
    if job.customer_id is None:
        return None
    accepted = db.execute(
        select(Estimate.id).where(
            Estimate.job_id == job.id,
            Estimate.status == "accepted",
            Estimate.deleted_at.is_(None),
        ).limit(1)
    ).first()
    if accepted is not None:
        return None

    inv = reuse_invoice
    if inv is None:
        live = db.execute(
            select(Invoice.id).where(
                Invoice.job_id == job.id,
                Invoice.deleted_at.is_(None),
                or_(Invoice.status.is_(None), Invoice.status != "void"),
            ).limit(1)
        ).first()
        if live is not None:
            return None
        customer_id = job.customer_id
        if not isinstance(customer_id, _uuid.UUID):
            try:
                customer_id = _uuid.UUID(str(customer_id))
            except (ValueError, AttributeError):
                return None
        # The shop's today, not the UTC server's — a closeout after ~7pm
        # Central auto-drafted an invoice dated tomorrow (#444).
        today = shop_today_from_settings(db)
        inv = Invoice(
            id=_uuid.uuid4(),
            job_id=job.id,
            invoice_number=next_invoice_number(db),
            billing_type="standard",
            sequence_number=1,
            subtotal=_money(0),
            tax_amount=_money(0),
            total=_money(0),
            balance_due=_money(0),
            status="draft",
            origin=AUTODRAFT_ORIGIN,
            invoice_date=today,
            due_date=today + timedelta(days=30),
            public_token=secrets.token_urlsafe(48)[:64],
            locked=False,
            customer_id=customer_id,
            company_id=str(tenant_id),
        )
        db.add(inv)
        db.flush()

    lines_added, lines_total, taxable_total = build_closeout_lines(
        db,
        tenant_id=str(tenant_id),
        invoice=inv,
        closeout=closeout,
        job_type=job.job_type,
        job_id=str(job.id),
    )
    if lines_added == 0 and reuse_invoice is None:
        db.delete(inv)
        return None

    # Tax (2026-08-08 audit): the autodraft was the ONLY creation path with
    # tax_rate NULL — the legacy flat-tax branch — so its parts were
    # structurally untaxable through every later office edit. Resolve the
    # same customer-aware rate the office create path uses; labor stays
    # untaxed via the line flags unless the tenant taxes labor.
    rate: Decimal | None = None
    try:
        from gdx_dispatch.modules.tax.service import resolve_rate  # noqa: PLC0415

        # SAVEPOINT-wrapped (GDXA-160). Be precise about what this buys, because
        # the first version of this comment overclaimed and an audit measured it
        # down: it does NOT save the tech's closeout. `closeout_job` already
        # flushes and calls this inside `db.begin_nested()`, and SQLAlchemy
        # recovers from the failed RELEASE by rolling back to that savepoint, so
        # the closeout committed either way (measured on PG 15.17: 1 survivor
        # pre-fix and post-fix). What it buys is the AUTODRAFT: without it the
        # whole draft is rolled away and logged `closeout_autodraft_failed`, so
        # Ready-for-Billing shows a blank form; with it the draft survives with
        # `tax_rate=None`, which is the documented degraded answer.
        #
        # It can also be inert. `build_closeout_lines` ran first, and its
        # `_load_tax_labor_flag` read is a deferred, unwrapped site — if THAT is
        # what failed, the transaction is already aborted and SAVEPOINT is
        # illegal on it, so this block raises at `__enter__` into the same
        # `except`. Contains the failure it can reach, not every failure.
        #
        # `resolve_rate` and its `is_customer_exempt` both RAISE on a DB error
        # rather than swallowing, which is what makes `contained_read` the right
        # tool here rather than the wrong one — its rule 5.
        with contained_read(db):
            candidate = resolve_rate(db, inv.customer_id)
        if candidate is not None and candidate > 0:
            rate = Decimal(str(candidate))
    except Exception:  # noqa: BLE001 — tax resolution must never block a closeout
        log.exception("autodraft_tax_resolve_failed job=%s", job.id)
        rate = None
    inv.tax_rate = rate

    if lines_total > 0:
        tax = _money(taxable_total * rate) if rate else _money(0)
        inv.subtotal = _money(lines_total)
        inv.tax_amount = tax
        inv.total = _money(lines_total + tax)
        inv.balance_due = inv.total
    return inv
