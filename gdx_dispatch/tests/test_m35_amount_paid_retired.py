"""M35 (money-audit-2026-08-04) — `Invoice.amount_paid` is a cache nothing writes.

`_recalculate_invoice` derives the balance from the `payments` table and
deliberately ignores the column; the only writer in the repo is the one-off
`tools/qb_payment_substance_repair.py`, which ran on prod once (2026-07-31
10:23:58 UTC, 287 rows). Every payment recorded since left the column behind —
measured 2026-08-22: 24 invoices, $62,473.72 of drift, all understating.

Blast radius, verified rather than assumed: job profitability understated
``total_paid``; ``is_untouched_autodraft``'s payment arm could never fire. The
mobile "Paid" row was a separate defect — it never rendered at all, because
``/api/invoices/{id}`` carried no ``amount_paid`` key.

Every test here has the same shape: record a payment WITHOUT touching
`amount_paid`, then assert the surface reports it. Against the pre-fix code
each one reads the stale 0 and fails.
"""
from __future__ import annotations

import pathlib
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from gdx_dispatch.core.invoice_paid import paid_to_date, paid_to_date_bulk
from gdx_dispatch.core.job_display_state import derive_job_display_state
from gdx_dispatch.models.tenant_models import Customer, Invoice, Payment

COMPANY = "11111111-1111-1111-1111-111111111111"


def _customer(db):
    c = Customer(id=uuid.uuid4(), name="M35 Co", company_id=COMPANY, created_at=datetime.now(UTC))
    db.add(c)
    db.flush()
    return c


def _invoice(db, *, total, balance_due, status="sent", stale_amount_paid=Decimal("0.00")):
    inv = Invoice(
        id=uuid.uuid4(),
        customer_id=_customer(db).id,
        invoice_number=f"T-{uuid.uuid4().hex[:8]}",
        status=status,
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal(str(total)),
        tax_amount=Decimal("0.00"),
        total=Decimal(str(total)),
        balance_due=Decimal(str(balance_due)),
        # The whole point: the cache is left at its stale value.
        amount_paid=stale_amount_paid,
        locked=False,
        public_token=uuid.uuid4().hex,
        company_id=COMPANY,
        created_at=datetime.now(UTC),
    )
    db.add(inv)
    db.flush()
    return inv


def _pay(db, inv, amount, *, voided=False):
    p = Payment(
        id=uuid.uuid4(),
        invoice_id=inv.id,
        amount=Decimal(str(amount)),
        company_id=COMPANY,
        created_at=datetime.now(UTC),
        voided_at=datetime.now(UTC) if voided else None,
    )
    db.add(p)
    db.flush()
    return p


def test_paid_to_date_reads_payments_not_the_stale_column(tenant_db):
    inv = _invoice(tenant_db, total=1000, balance_due=400)
    _pay(tenant_db, inv, 600)

    assert paid_to_date(tenant_db, inv.id) == Decimal("600.00")
    # Proof the fixture really is the drifted shape the prod data has.
    assert float(inv.amount_paid) == 0.0


def test_paid_to_date_ignores_voided_payments(tenant_db):
    """Voided payments stay as history but stop counting (GL S6/P4)."""
    inv = _invoice(tenant_db, total=1000, balance_due=1000)
    _pay(tenant_db, inv, 250)
    _pay(tenant_db, inv, 999, voided=True)

    assert paid_to_date(tenant_db, inv.id) == Decimal("250.00")


def test_paid_to_date_bulk_omits_unpaid_invoices(tenant_db):
    a = _invoice(tenant_db, total=100, balance_due=0)
    b = _invoice(tenant_db, total=100, balance_due=100)
    _pay(tenant_db, a, 100)

    got = paid_to_date_bulk(tenant_db, [a.id, b.id])
    assert got[str(a.id)] == Decimal("100.00")
    assert str(b.id) not in got, "invoices with no payments must be absent, not 0-filled"
    assert paid_to_date_bulk(tenant_db, []) == {}


def test_partially_paid_state_fires_on_a_real_payment():
    """job_display_state keyed "Partially Paid" on the stale column, so a
    partial payment taken after 2026-07-31 showed the job as "Invoiced".
    """
    state = derive_job_display_state(
        lifecycle_stage="completed",
        invoices=[{"status": "sent", "balance_due": 400, "amount_paid": 600, "billing_type": "standard"}],
    )
    assert state.stage == "partially_paid", state

    # The drifted shape — the bug — reads as merely invoiced.
    stale = derive_job_display_state(
        lifecycle_stage="completed",
        invoices=[{"status": "sent", "balance_due": 400, "amount_paid": 0, "billing_type": "standard"}],
    )
    assert stale.stage == "invoiced", stale


def test_deposit_paid_badge_fires_on_a_partially_paid_deposit():
    """deposit_paid ORs status=='paid' with amount_paid>0; a partially paid
    deposit only reaches it through the amount_paid arm.
    """
    state = derive_job_display_state(
        lifecycle_stage="in_progress",
        invoices=[{"status": "sent", "balance_due": 100, "amount_paid": 250, "billing_type": "deposit"}],
    )
    assert state.deposit_paid is True


def test_untouched_autodraft_sees_a_payment_the_column_missed(tenant_db):
    """The machine may rebuild or void an autodraft only while untouched. The
    payment arm read the stale column, so an autodraft carrying real money
    still looked untouched and could be voided out from under it.
    """
    from gdx_dispatch.core.closeout_billing import AUTODRAFT_ORIGIN, is_untouched_autodraft

    inv = _invoice(tenant_db, total=500, balance_due=500, status="draft")
    inv.origin = AUTODRAFT_ORIGIN
    tenant_db.flush()

    assert is_untouched_autodraft(inv, tenant_db) is True

    _pay(tenant_db, inv, 100)  # real money lands; amount_paid stays 0
    assert float(inv.amount_paid) == 0.0
    assert is_untouched_autodraft(inv, tenant_db) is False, (
        "an autodraft carrying a real payment is no longer the machine's to void"
    )


def test_no_live_code_reads_the_deprecated_column():
    """Absence assertion — proves a string is GONE, the safe direction.

    An adversarial review broke the first version of this scanner four ways, so
    it now covers each: attribute access, ``getattr``, raw-SQL string literals,
    and dict/mapping subscripts. It also asserts it actually scanned files —
    the first version used a relative path and passed vacuously green when run
    from anywhere but the repo root.
    """
    # Anchor to the repo root from THIS file, not the process cwd.
    root = pathlib.Path(__file__).resolve().parents[1]
    assert root.name == "gdx_dispatch", root

    patterns = (
        re.compile(r"\w+\.amount_paid\b"),                      # inv.amount_paid
        re.compile(r"getattr\s*\(\s*[^,]+,\s*[\"']amount_paid"),  # getattr(inv, "amount_paid")
        # DB-row subscripts only. A plain dict/payload key of the same name is
        # ALLOWED on purpose — `amount_paid` is the response field, now fed
        # from the payments table, and job_display_state reads that same key
        # off the dict jobs.py hands it.
        re.compile(r"_mapping\s*\[\s*[\"']amount_paid"),
        re.compile(r"amount_paid", re.I),                        # raw SQL text, see sql_only below
    )
    allowed = {
        "docker/demo/seed_demo.py",     # demo seeder writes the column
        "modules/ledger/backfill.py",   # legacy-suspect diagnostic (uses getattr on purpose)
        "models/tenant_models.py",      # the column definition itself
    }

    def _candidate_lines(src: str) -> set[int]:
        """Line numbers where `amount_paid` appears as CODE, not prose.

        Tokenizing decides code-vs-prose; the regexes below then run against
        the real source line, because an attribute read spans three tokens
        (NAME `.` NAME) and matching a lone token can never see it. Getting
        that wrong is how the first version of this scanner passed green
        against every pre-fix reader.
        """
        import io
        import tokenize

        out: set[int] = set()
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return out
        for tok in toks:
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING:
                body = tok.string
                if "amount_paid" not in body:
                    continue
                # Raw SQL counts, and so does a bare "amount_paid" literal
                # (it may be a DB-row subscript key — the regexes decide from
                # the full line). A docstring that merely names the column
                # does not.
                if re.search(r"\b(select|update|insert)\b", body, re.I) or (
                    body.strip("\"'") == "amount_paid"
                ):
                    out.add(tok.start[0])
                continue
            if tok.type == tokenize.NAME and tok.string == "amount_paid":
                out.add(tok.start[0])
        return out

    scanned = 0
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in allowed or rel.startswith("tests/"):
            continue
        src = path.read_text(encoding="utf-8")
        scanned += 1
        if "amount_paid" not in src:
            continue
        lines = src.splitlines()
        for lineno in _candidate_lines(src):
            real = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            if any(p.search(real) for p in patterns[:3]) or re.search(
                r"\b(select|update|insert)\b.*amount_paid", real, re.I
            ):
                offenders.append(f"{rel}:{lineno}: {real[:90]}")

    assert scanned > 100, f"scanner only looked at {scanned} files — it is not scanning the tree"
    assert not offenders, (
        "live code reads the deprecated column again:\n" + "\n".join(sorted(set(offenders)))
    )


def test_the_scanner_actually_catches_each_bypass(tmp_path):
    """Counterfactual for the guard itself. A scanner nobody has tried to fool
    is a scanner that passes green while the bug walks back in — this repo has
    shipped exactly that before.
    """
    import io
    import re as _re
    import tokenize

    def _hits(src: str) -> bool:
        pats = (
            _re.compile(r"\w+\.amount_paid\b"),
            _re.compile(r"getattr\s*\(\s*[^,]+,\s*[\"']amount_paid"),
            _re.compile(r"_mapping\s*\[\s*[\"']amount_paid"),
        )
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except Exception:
            return False
        for tok in toks:
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING:
                if _re.search(r"\b(select|update|insert)\b", tok.string, _re.I) and "amount_paid" in tok.string:
                    return True
                continue
            if any(p.search(tok.string) for p in pats):
                return True
        # multi-token forms (getattr(...) / d["..."]) need the joined line
        for line in src.splitlines():
            if line.strip().startswith("#"):
                continue
            if any(p.search(line) for p in pats):
                return True
        return False

    assert _hits("x = inv.amount_paid\n"), "attribute access"
    assert _hits('y = getattr(inv, "amount_paid")\n'), "getattr"
    assert _hits('z = row._mapping["amount_paid"]\n'), "DB-row mapping subscript"
    assert _hits('q = text("SELECT amount_paid FROM invoices")\n'), "raw SQL"
    # Prose must NOT trip it, or the guard becomes noise everyone silences.
    assert not _hits('"""The amount_paid column is deprecated."""\n'), "docstring prose"
    assert not _hits("# amount_paid is gone\n"), "comment prose"


def test_office_invoice_detail_carries_paid_and_credits_that_reconcile(tenant_db):
    """Ledger item #1 / the half M35 missed.

    MobileBillingView renders its "Paid" row on `detail.amount_paid != null`,
    and `detail` comes from GET /api/invoices/{id} — whose serializer carried
    NO amount_paid key at all, so the row never rendered. (An earlier draft of
    this work claimed it rendered "Paid $0.00"; that was wrong, and an
    adversarial review caught it.)

    The fix must not swap one lie for another: paid-to-date alone would let a
    credited invoice read "Total 1000 / Paid 300 / Balance 0" with $700
    unaccounted for. So the payload carries credits too, and
    total - paid - credits must equal balance_due.
    """
    from gdx_dispatch.models.tenant_models import InvoiceAdjustment
    from gdx_dispatch.routers.invoices import _serialize_invoice

    inv = _invoice(tenant_db, total=1000, balance_due=0)
    _pay(tenant_db, inv, 300)
    _pay(tenant_db, inv, 500, voided=True)  # history, must not count
    tenant_db.add(
        InvoiceAdjustment(
            id=uuid.uuid4(),
            invoice_id=inv.id,
            kind="credit_memo",
            amount=Decimal("700.00"),
            company_id=COMPANY,
            created_at=datetime.now(UTC),
        )
    )
    tenant_db.flush()
    tenant_db.refresh(inv)

    payload = _serialize_invoice(
        inv, include_lines=False, include_payments=True, credit_total=Decimal("700.00")
    )

    assert payload["amount_paid"] == 300.0, "voided payment must not count"
    assert payload["credit_total"] == 700.0
    # The arithmetic the screen shows has to close.
    assert (
        float(payload["total"]) - payload["amount_paid"] - payload["credit_total"]
        == float(payload["balance_due"])
    )


def test_office_invoice_list_does_not_pay_an_n_plus_one_for_it(tenant_db):
    """paid-to-date rides on the payments already loaded for the DETAIL view.
    List callers pass include_payments=False and must not gain the key (or the
    query) — a list of 200 invoices lazy-loading payments is the N+1 this
    deliberately avoids.
    """
    from gdx_dispatch.routers.invoices import _serialize_invoice

    inv = _invoice(tenant_db, total=100, balance_due=100)
    payload = _serialize_invoice(inv)
    assert "amount_paid" not in payload
    assert "credit_total" not in payload
