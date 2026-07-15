"""GL Phase 1 (S3) — posting engine core: money, keys, post/reverse.

Plan gates (§S3): sum-to-zero property; allocate exactness incl. non-even;
key liveness (A→B→A → exactly one live entry at content A); same-state
replay → identical ledger. Synthetic events only — no operational writers
exist until S4/S5. SQLite; the PG triggers backstopping these invariants are
covered by test_gl_triggers.py.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.modules.ledger.coa import LedgerConfigError, seed_coa
from gdx_dispatch.modules.ledger.engine import (
    PeriodLockedError,
    PostingEvent,
    PostingLine,
    UnbalancedEntryError,
    post_for_event,
    reverse_entry,
)
from gdx_dispatch.modules.ledger.keys import content_hash, reversal_key
from gdx_dispatch.modules.ledger.models import (
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
)
from gdx_dispatch.modules.ledger.money import allocate, to_cents

COMPANY = "11111111-1111-1111-1111-111111111111"
DAY = dt.date(2026, 7, 1)


@pytest.fixture
def ledger_db(tenant_db):
    seed_coa(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _event(amount=10_000, event="issued", source_id="inv-1", effective=DAY, **kw):
    return PostingEvent(
        company_id=COMPANY,
        source_type="invoice",
        source_id=source_id,
        event=event,
        effective_at=effective,
        lines=(
            PostingLine(amount_cents=amount, role="AR"),
            PostingLine(amount_cents=-amount, role="SALES_FALLBACK"),
        ),
        **kw,
    )


def _entries(db):
    return db.scalars(select(GlJournalEntry).order_by(GlJournalEntry.entry_no)).all()


def _ledger_snapshot(db):
    """(key, status, sorted line tuples) for every entry — replay identity."""
    snap = []
    for e in _entries(db):
        lines = sorted(
            (str(l.account_id), l.amount_cents)
            for l in db.scalars(
                select(GlJournalLine).where(GlJournalLine.entry_id == e.id)
            )
        )
        snap.append((e.idempotency_key, e.status, tuple(lines)))
    return snap


# ---------------------------------------------------------------------------
# money.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("0.01", 1),
        ("1.005", 101),      # ROUND_HALF_UP at the half-cent
        ("2.674", 267),
        ("2.675", 268),
        ("-1.005", -101),    # half-up = away from zero for negatives (Decimal)
        (Decimal("19.99"), 1999),
        (7, 700),
        ("0", 0),
    ],
)
def test_to_cents(raw, cents):
    assert to_cents(raw) == cents


def test_to_cents_rejects_float():
    with pytest.raises(TypeError, match="float"):
        to_cents(1.005)


@settings(max_examples=200, deadline=None)
@given(
    total=st.integers(min_value=-10_000_000, max_value=10_000_000),
    weights=st.lists(st.integers(min_value=0, max_value=1_000_000), min_size=1, max_size=12).filter(
        lambda ws: sum(ws) > 0
    ),
)
def test_allocate_sum_preserving_and_fair(total, weights):
    parts = allocate(total, weights)
    assert sum(parts) == total                          # the invariant
    assert len(parts) == len(weights)
    wsum = sum(weights)
    for part, w in zip(parts, weights):
        exact = Decimal(total) * w / wsum
        assert abs(Decimal(part) - exact) < 1           # within one cent of exact
    assert allocate(total, weights) == parts            # deterministic


def test_allocate_non_even_split_exact():
    # 100 cents over equal thirds: largest-remainder → 34/33/33
    assert allocate(100, [1, 1, 1]) == [34, 33, 33]
    # negative mirrors the positive allocation
    assert allocate(-100, [1, 1, 1]) == [-34, -33, -33]


def test_allocate_rejects_bad_input():
    with pytest.raises(ValueError):
        allocate(100, [])
    with pytest.raises(ValueError):
        allocate(100, [0, 0])
    assert allocate(0, [0, 0]) == [0, 0]
    with pytest.raises(TypeError):
        allocate(100, [1.5, 2.5])
    with pytest.raises(ValueError):
        allocate(100, [-1, 2])


def test_to_cents_nonfinite_raises_value_error():
    for bad in ("Infinity", "NaN", "1e30", "garbage"):
        with pytest.raises(ValueError):
            to_cents(bad)


def test_allocate_exact_beyond_decimal_context():
    """Audit round 1: Decimal's 28-digit context silently broke the invariant
    at absurd magnitudes; Fraction arithmetic is exact at any scale."""
    total = 10**29
    parts = allocate(total, [1, 1, 1])
    assert sum(parts) == total
    parts = allocate(-(10**29) - 7, [3, 1, 5])
    assert sum(parts) == -(10**29) - 7


def test_content_hash_rejects_ambiguous_types():
    """Hash identity must be economic identity — repr-based aliasing
    (Decimal("10") vs "10", set ordering, int keys coerced to str) is
    rejected loudly instead of hashed wrongly."""
    with pytest.raises(TypeError, match="float"):
        content_hash({"lines": [{"amount": 10.5}]})
    with pytest.raises(TypeError, match="Decimal"):
        content_hash({"amount": Decimal("10")})
    with pytest.raises(TypeError, match="set"):
        content_hash({"tags": {"a", "b"}})
    with pytest.raises(TypeError, match="non-str mapping key"):
        content_hash({"lines": [{1: "x"}]})
    # the canonical scalar set round-trips fine
    assert content_hash({"a": 1, "b": None, "c": True, "d": "x", "e": [1, "2"]})


# ---------------------------------------------------------------------------
# post_for_event — balance validation (sum-to-zero property)
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(amounts=st.lists(st.integers(min_value=1, max_value=500_000), min_size=1, max_size=7))
def test_balanced_by_construction_always_validates(amounts):
    """Balanced sets built by construction (audit round 1: random ints
    summing to zero is fuzzer luck ~1e-6 — construct the balance instead)."""
    lines = tuple(
        PostingLine(amount_cents=a, role="AR") for a in amounts
    ) + (PostingLine(amount_cents=-sum(amounts), role="SALES_FALLBACK"),)
    from gdx_dispatch.modules.ledger.engine import _validate_balance
    _validate_balance(lines)  # must not raise


@settings(max_examples=100, deadline=None)
@given(amounts=st.lists(st.integers(min_value=-500_000, max_value=500_000).filter(bool), min_size=2, max_size=8))
def test_unbalanced_line_sets_rejected(amounts):
    lines = tuple(
        PostingLine(amount_cents=a, role="AR" if i % 2 else "SALES_FALLBACK")
        for i, a in enumerate(amounts)
    )
    balanced = sum(amounts) == 0 and any(a > 0 for a in amounts) and any(a < 0 for a in amounts)
    from gdx_dispatch.modules.ledger.engine import _validate_balance
    if balanced:
        _validate_balance(lines)
    else:
        with pytest.raises(UnbalancedEntryError):
            _validate_balance(lines)


@pytest.mark.parametrize(
    "lines",
    [
        (PostingLine(amount_cents=100, role="AR"),),                                  # 1 line
        (PostingLine(amount_cents=100, role="AR"), PostingLine(amount_cents=-50, role="SALES_FALLBACK")),  # unbalanced
        (PostingLine(amount_cents=100, role="AR"), PostingLine(amount_cents=100, role="SALES_FALLBACK")),  # no credit
        (PostingLine(amount_cents=100), PostingLine(amount_cents=-100, role="SALES_FALLBACK")),            # neither role nor id
    ],
)
def test_bad_line_sets_rejected(ledger_db, lines):
    event = PostingEvent(
        company_id=COMPANY, source_type="invoice", source_id="i", event="issued",
        effective_at=DAY, lines=lines,
    )
    with pytest.raises(UnbalancedEntryError):
        post_for_event(ledger_db, event)
    assert _entries(ledger_db) == []


# ---------------------------------------------------------------------------
# post_for_event — happy path, idempotency, liveness
# ---------------------------------------------------------------------------

def test_post_resolves_roles_and_balances(ledger_db):
    entry = post_for_event(ledger_db, _event())
    ledger_db.commit()

    assert entry.status == "posted"
    assert entry.idempotency_key.startswith("invoice:inv-1:issued:")
    assert entry.idempotency_key.endswith(":0")
    lines = ledger_db.scalars(
        select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)
    ).all()
    assert sorted(l.amount_cents for l in lines) == [-10_000, 10_000]
    assert entry.entry_no == 1


def test_plain_retry_returns_same_live_entry(ledger_db):
    first = post_for_event(ledger_db, _event())
    ledger_db.commit()
    again = post_for_event(ledger_db, _event())
    assert again.id == first.id
    assert len(_entries(ledger_db)) == 1


def test_a_b_a_liveness(ledger_db):
    """Edit content A→B→A: exactly one LIVE entry at content A, keys never
    collide with the reversed one (seq = count of reversed at prefix)."""
    a1 = post_for_event(ledger_db, _event(amount=10_000))
    reverse_entry(ledger_db, a1)
    b = post_for_event(ledger_db, _event(amount=20_000))
    reverse_entry(ledger_db, b)
    a2 = post_for_event(ledger_db, _event(amount=10_000))
    ledger_db.commit()

    assert a2.id != a1.id
    assert a1.idempotency_key.endswith(":0")
    assert a2.idempotency_key.endswith(":1")
    assert a1.idempotency_key.rsplit(":", 1)[0] == a2.idempotency_key.rsplit(":", 1)[0]

    live = [e for e in _entries(ledger_db) if e.status == "posted" and e.reverses_entry_id is None]
    assert [e.id for e in live] == [a2.id]

    # retry of A after the whole dance still lands on the live entry
    assert post_for_event(ledger_db, _event(amount=10_000)).id == a2.id


def test_same_state_replay_is_identical(ledger_db):
    """Replaying every event against the same source state mints identical
    keys and creates nothing new (backfill re-runnability)."""
    events = [
        _event(amount=10_000, source_id="inv-1"),
        _event(amount=5_500, source_id="inv-2"),
        _event(amount=125, source_id="inv-3", event="adjusted"),
    ]
    for e in events:
        post_for_event(ledger_db, e)
    reverse_entry(ledger_db, _entries(ledger_db)[1])  # a reversal in the mix
    ledger_db.commit()
    before = _ledger_snapshot(ledger_db)

    for e in events:
        post_for_event(ledger_db, e)  # inv-2 is reversed → seq bumps → reposts
    ledger_db.commit()
    after = _ledger_snapshot(ledger_db)

    # the only delta allowed: inv-2's repost (its live entry had been reversed)
    new = [s for s in after if s not in before]
    assert len(new) == 1 and new[0][0].endswith(":1")
    # replaying AGAIN with this state changes nothing at all
    for e in events:
        post_for_event(ledger_db, e)
    ledger_db.commit()
    assert _ledger_snapshot(ledger_db) == after


def test_different_content_same_source_gets_new_key(ledger_db):
    e1 = post_for_event(ledger_db, _event(amount=10_000))
    e2 = post_for_event(ledger_db, _event(amount=10_001))
    assert e1.id != e2.id
    assert e1.idempotency_key != e2.idempotency_key


def test_explicit_account_id_lines_post(ledger_db):
    from gdx_dispatch.modules.ledger.models import GlAccount
    fuel = ledger_db.scalars(select(GlAccount).where(GlAccount.code == "6100")).one()
    bank = ledger_db.scalars(select(GlAccount).where(GlAccount.code == "1000")).one()
    event = PostingEvent(
        company_id=COMPANY, source_type="expense", source_id="x1", event="recorded",
        effective_at=DAY,
        lines=(
            PostingLine(amount_cents=4_200, account_id=fuel.id),
            PostingLine(amount_cents=-4_200, account_id=bank.id),
        ),
    )
    entry = post_for_event(ledger_db, event)
    assert entry.status == "posted"


def test_deactivated_account_rejected_for_new_postings(ledger_db):
    from gdx_dispatch.modules.ledger.models import GlAccount
    fuel = ledger_db.scalars(select(GlAccount).where(GlAccount.code == "6100")).one()
    fuel.active = False
    ledger_db.flush()
    event = PostingEvent(
        company_id=COMPANY, source_type="expense", source_id="x2", event="recorded",
        effective_at=DAY,
        lines=(
            PostingLine(amount_cents=100, account_id=fuel.id),
            PostingLine(amount_cents=-100, role="OPERATING_BANK"),
        ),
    )
    with pytest.raises(LedgerConfigError, match="deactivated"):
        post_for_event(ledger_db, event)


# ---------------------------------------------------------------------------
# reverse_entry
# ---------------------------------------------------------------------------

def test_reversal_negates_lines_and_marks_original(ledger_db):
    entry = post_for_event(ledger_db, _event())
    reversal = reverse_entry(ledger_db, entry, created_by="u1")
    ledger_db.commit()

    assert entry.status == "reversed"
    assert entry.reversed_by_entry_id == reversal.id
    assert reversal.reverses_entry_id == entry.id
    assert reversal.idempotency_key == reversal_key(entry.id)

    orig = {(str(l.account_id), l.amount_cents) for l in ledger_db.scalars(
        select(GlJournalLine).where(GlJournalLine.entry_id == entry.id))}
    mirror = {(str(l.account_id), -l.amount_cents) for l in ledger_db.scalars(
        select(GlJournalLine).where(GlJournalLine.entry_id == reversal.id))}
    assert orig == mirror


def test_reverse_is_idempotent(ledger_db):
    entry = post_for_event(ledger_db, _event())
    r1 = reverse_entry(ledger_db, entry)
    r2 = reverse_entry(ledger_db, entry)
    assert r1.id == r2.id
    assert len(_entries(ledger_db)) == 2


def test_reversal_of_deactivated_account_still_works(ledger_db):
    """Unwinding must never be blocked by later CoA edits: reversal mirrors
    raw account ids, skipping active-account validation."""
    from gdx_dispatch.modules.ledger.models import GlAccount
    entry = post_for_event(ledger_db, _event())
    ar = ledger_db.scalars(select(GlAccount).where(GlAccount.code == "1200")).one()
    ar.active = False
    ledger_db.flush()
    reversal = reverse_entry(ledger_db, entry)
    assert reversal.status == "posted"


# ---------------------------------------------------------------------------
# period locks (§3.6)
# ---------------------------------------------------------------------------

def _lock(db, through: dt.date):
    db.add(GlPeriodLock(lock_date=through, company_id=COMPANY))
    db.flush()


def test_posting_into_locked_period_blocked(ledger_db):
    _lock(ledger_db, DAY)
    with pytest.raises(PeriodLockedError):
        post_for_event(ledger_db, _event(effective=DAY))
    # first open day is fine
    assert post_for_event(ledger_db, _event(effective=DAY + dt.timedelta(days=1))).status == "posted"


def test_lock_override_posts_and_audit_logs(ledger_db):
    _lock(ledger_db, DAY)
    entry = post_for_event(
        ledger_db, _event(effective=DAY, created_by="closer"), override_lock=True
    )
    ledger_db.commit()
    assert entry.status == "posted"
    log = ledger_db.scalars(
        select(AuditLog).where(AuditLog.action == "gl_posted_into_locked_period")
    ).one()
    assert log.user_id == "closer"
    assert log.details["lock_date"] == str(DAY)


def test_latest_lock_wins(ledger_db):
    _lock(ledger_db, DAY - dt.timedelta(days=30))
    _lock(ledger_db, DAY)  # later, tighter lock
    with pytest.raises(PeriodLockedError):
        post_for_event(ledger_db, _event(effective=DAY))


def test_reversal_respects_period_lock(ledger_db):
    entry = post_for_event(ledger_db, _event(effective=DAY))
    _lock(ledger_db, DAY)
    with pytest.raises(PeriodLockedError):
        reverse_entry(ledger_db, entry)  # defaults to original (locked) date
    # unwind into the open period instead
    reversal = reverse_entry(ledger_db, entry, effective_at=DAY + dt.timedelta(days=1))
    assert reversal.status == "posted"


# ---------------------------------------------------------------------------
# key collision handling — force the REAL IntegrityError branches
# (audit round 1: the pre-flight check was dodging every collision path)
# ---------------------------------------------------------------------------

def test_preflight_returns_posted_entry_without_insert(ledger_db):
    first = post_for_event(ledger_db, _event())
    ledger_db.commit()
    assert post_for_event(ledger_db, _event()).id == first.id
    assert len(_entries(ledger_db)) == 1


def test_integrity_collision_with_posted_entry_returns_it(ledger_db, monkeypatch):
    """Deterministic race simulation: blind the pre-flight existence check
    once, so the engine's INSERT genuinely hits the unique index, rolls back
    to the savepoint, re-queries, and returns the posted winner."""
    import gdx_dispatch.modules.ledger.engine as engine_mod

    first = post_for_event(ledger_db, _event())
    ledger_db.commit()

    real = engine_mod._entry_by_key
    calls = {"n": 0}

    def blind_once(session, company_id, key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pre-flight misses, like a not-yet-visible writer
        return real(session, company_id, key)

    monkeypatch.setattr(engine_mod, "_entry_by_key", blind_once)
    again = post_for_event(ledger_db, _event())
    assert again.id == first.id
    assert calls["n"] >= 2  # the except-IntegrityError branch re-queried
    # the enclosing transaction survived the savepoint rollback
    other = post_for_event(ledger_db, _event(source_id="inv-9"))
    ledger_db.commit()
    assert other.status == "posted"
    assert len(_entries(ledger_db)) == 2


def test_reversed_collision_recomputes_seq_and_retries(ledger_db, monkeypatch):
    """Stale-seq race: the first computed key lands on a REVERSED entry —
    insert collides, the engine must recompute seq and post at the next one."""
    import gdx_dispatch.modules.ledger.engine as engine_mod

    a1 = post_for_event(ledger_db, _event())
    reverse_entry(ledger_db, a1)
    ledger_db.commit()

    real = engine_mod.compute_seq
    calls = {"n": 0}

    def stale_once(session, company_id, prefix):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0  # stale: pretends no reversed entries exist yet
        return real(session, company_id, prefix)

    monkeypatch.setattr(engine_mod, "compute_seq", stale_once)
    a2 = post_for_event(ledger_db, _event())
    ledger_db.commit()
    assert a2.id != a1.id
    assert a2.idempotency_key.endswith(":1")
    assert calls["n"] >= 2


def test_key_attempt_exhaustion_raises(ledger_db, monkeypatch):
    """A seq that never advances (someone reversing as fast as we post) must
    surface as a loud RuntimeError, not an infinite loop."""
    import gdx_dispatch.modules.ledger.engine as engine_mod

    a1 = post_for_event(ledger_db, _event())
    reverse_entry(ledger_db, a1)
    ledger_db.commit()

    monkeypatch.setattr(engine_mod, "compute_seq", lambda *a: 0)  # forever stale
    with pytest.raises(RuntimeError, match="could not settle idempotency key"):
        post_for_event(ledger_db, _event())


# ---------------------------------------------------------------------------
# key-format injectivity (audit round 1: ':' in components collides prefixes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kw",
    [
        {"source_id": "42:issued:aaaaaaaaaaaaaaaa"},
        {"source_type": "in:voice"},
        {"event": "is:sued"},
        {"source_id": ""},
        {"source_type": "reversal"},
    ],
)
def test_colon_or_reserved_key_components_rejected(ledger_db, kw):
    base = dict(
        company_id=COMPANY, source_type="invoice", source_id="inv-1",
        event="issued", effective_at=DAY,
        lines=(
            PostingLine(amount_cents=100, role="AR"),
            PostingLine(amount_cents=-100, role="SALES_FALLBACK"),
        ),
    )
    base.update(kw)
    with pytest.raises(ValueError):
        post_for_event(ledger_db, PostingEvent(**base))
    assert _entries(ledger_db) == []
