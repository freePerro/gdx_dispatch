"""MH-6 — `/api/estimates` list response must include `customer_name`.

Audit P1 #8 (mobile UX audit 2026-05-19): every estimate card on
`/mobile/estimates` rendered '—' for customer because the serializer
only carried `customer_id`. The view's fallback chain
`e.customer_name || e.customer?.name || '—'` fell all the way through.

Tests at the HTTP layer via the existing fixture set so we exercise
the enrichment path through SQLAlchemy + the live ORM relationships,
not a unit-level mock that would let a future refactor silently break
the join.
"""
from __future__ import annotations

import importlib
import inspect


def test_list_estimates_serializer_block_includes_customer_name_enrichment():
    """The list_estimates handler must contain the customer_name enrichment
    block — locked at the source-text level so a regression that strips
    the block (e.g. a refactor) fails LOCALLY rather than re-surfacing
    on the next prod walk.
    """
    estimates_mod = importlib.import_module("gdx_dispatch.routers.estimates")
    src = inspect.getsource(estimates_mod.list_estimates)
    assert "customer_name" in src, (
        "list_estimates must enrich items with `customer_name` — audit P1 #8 "
        "regression guard. See `_serialize_estimate` (which intentionally "
        "leaves customer_name out of the row-level serializer) and the "
        "enrichment block right after `items = [_serialize_estimate(...)]`."
    )
    # Lock the fallback pattern shape — Estimate.customer_id first, Job
    # fallback for QB-null historical imports (mirrors invoices.py).
    assert "Estimate.customer_id" in src or "row.customer_id" in src
    assert "Job" in src, "must fall back through Job.customer_id for QB-null estimates"


def test_serialize_estimate_does_not_carry_customer_name_alone():
    """The row-level serializer deliberately does NOT include customer_name
    on its own (the enrichment is batched at the list level for query
    efficiency). Single-estimate GET /{id} is unchanged by MH-6; this
    test locks that boundary so a well-meaning future refactor doesn't
    accidentally move the join into the per-row path and N+1 the list.
    """
    estimates_mod = importlib.import_module("gdx_dispatch.routers.estimates")
    src = inspect.getsource(estimates_mod._serialize_estimate)
    assert "customer_name" not in src, (
        "_serialize_estimate must remain customer-name-free; enrichment "
        "happens at the list level. If you genuinely need customer_name "
        "on the single-GET, do it in the GET handler the same way "
        "list_estimates does (batched, not per-row)."
    )


# ── Behavioral tests (mocked DB, exercise the runtime — NOT source greps) ──

class _StubRow:
    """Mimics SQLAlchemy result-row .all() shape: tuples of (col, col, ...)."""

    def __init__(self, *vals):
        self.vals = vals

    def __iter__(self):
        return iter(self.vals)


class _StubResult:
    def __init__(self, rows):
        self._rows = rows
        self._scalars = None

    def all(self):
        return [tuple(r) for r in self._rows]

    def scalars(self):
        # For the `db.execute(q).scalars().all()` chain returning Estimates.
        self._scalars = _StubScalars(self._rows)
        return self._scalars


class _StubScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeEstimate:
    """Just enough of the Estimate ORM shape for _serialize_estimate +
    the enrichment to walk it."""

    def __init__(self, id_, customer_id=None, job_id=None, total=0):
        from datetime import datetime, timezone
        self.id = id_
        self.customer_id = customer_id
        self.job_id = job_id
        self.estimate_number = f"EST-{id_}"
        self.label = None
        self.jobsite_address = None
        self.description = None
        self.notes = None
        self.tax_rate = None
        self.discount = None
        self.status = "draft"
        self.total = total
        self.sent_at = None
        self.accepted_at = None
        self.declined_at = None
        self.declined_reason = None
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = None
        self.deleted_at = None
        self.lines = []


def _build_db_stub(estimates, cust_rows, job_rows):
    """Return a db-like object where .execute(stmt) dispatches based on
    the SQL text of the statement (not call order), so conditional
    branches in the handler — e.g. skipping the Customer query when
    `cust_ids` is empty — still produce the right row set for whichever
    query DOES fire."""
    call_log = []

    class _DB:
        def execute(self, stmt, *_, **__):
            call_log.append(stmt)
            # Use the compiled SQL representation as the dispatch key.
            sql = str(stmt).lower()
            if "from estimates" in sql:
                return _StubResult(estimates)
            if "from jobs" in sql or " join customers " in sql:
                # The Job → Customer join query (selects Job.id, Customer.name).
                return _StubResult(job_rows)
            if "from customers" in sql:
                # The direct Customer SELECT (selects Customer.id, name).
                return _StubResult(cust_rows)
            return _StubResult([])

    return _DB(), call_log


def test_enrichment_uses_estimate_customer_id_when_available():
    """Happy path: Estimate.customer_id points to a live customer."""
    from gdx_dispatch.routers.estimates import list_estimates

    est = _FakeEstimate("e1", customer_id="cust-1", job_id="job-1")
    db, _ = _build_db_stub(
        estimates=[est],
        cust_rows=[("cust-1", "Acme Co.")],
        job_rows=[],
    )
    items = list_estimates(job_id=None, customer_id=None, _={}, db=db)
    assert items[0]["customer_name"] == "Acme Co."


def test_enrichment_falls_back_via_job_when_customer_id_is_null():
    """QB-import case: Estimate.customer_id is NULL, linkage is on Job."""
    from gdx_dispatch.routers.estimates import list_estimates

    est = _FakeEstimate("e2", customer_id=None, job_id="job-9")
    db, _ = _build_db_stub(
        estimates=[est],
        cust_rows=[],
        job_rows=[("job-9", "QB-Imported Customer")],
    )
    items = list_estimates(job_id=None, customer_id=None, _={}, db=db)
    assert items[0]["customer_name"] == "QB-Imported Customer"


def test_enrichment_falls_back_via_job_when_customer_id_is_stale():
    """MH-6 audit round-1 catch: pre-fix the if/elif partition routed an
    estimate to the customer-id query OR the job-id query — never both.
    An Estimate whose `customer_id` points to a soft-deleted/orphaned
    customer (no row in the Customer SELECT) would render '—' even when
    the live link sits on the Job. Round-1 fix: build BOTH maps for
    every row, then pick the first that returns a name.

    This test exercises that exact failure mode.
    """
    from gdx_dispatch.routers.estimates import list_estimates

    est = _FakeEstimate("e3", customer_id="ghost-cust", job_id="job-3")
    db, _ = _build_db_stub(
        estimates=[est],
        cust_rows=[],  # ghost-cust returns nothing — soft-deleted
        job_rows=[("job-3", "Real Customer Via Job")],
    )
    items = list_estimates(job_id=None, customer_id=None, _={}, db=db)
    assert items[0]["customer_name"] == "Real Customer Via Job", (
        "When Estimate.customer_id resolves to no live row, the enrichment "
        "must fall through to the Job lookup — not leave the card showing "
        "'—' the way the audit caught."
    )


def test_enrichment_leaves_customer_name_absent_when_neither_path_resolves():
    """No customer_id AND no job linkage — the card should render '—' on
    the frontend, NOT echo an empty string from the backend (which the
    frontend's `|| '—'` chain would still handle, but absent is cleaner).
    """
    from gdx_dispatch.routers.estimates import list_estimates

    est = _FakeEstimate("e4", customer_id=None, job_id=None)
    db, _ = _build_db_stub(
        estimates=[est],
        cust_rows=[],
        job_rows=[],
    )
    items = list_estimates(job_id=None, customer_id=None, _={}, db=db)
    assert "customer_name" not in items[0], (
        "When neither customer_id nor job linkage resolves, the key must "
        "be absent (not empty-string). Frontend fallback to '—' is the "
        "intentional surface for this case."
    )


def test_enrichment_failure_returns_list_without_5xx():
    """A DB exception during the enrichment block must NOT take down the
    list response. The /mobile/estimates view stays functional, just
    without customer names (cards render '—' until the next request)."""
    from gdx_dispatch.routers.estimates import list_estimates

    est = _FakeEstimate("e5", customer_id="x", job_id="y")
    call_count = [0]

    class _BrokenDB:
        def execute(self, *_, **__):
            call_count[0] += 1
            if call_count[0] == 1:
                return _StubResult([est])  # initial scalars() works
            raise RuntimeError("simulated DB outage on the enrich query")

    items = list_estimates(job_id=None, customer_id=None, _={}, db=_BrokenDB())
    assert isinstance(items, list)
    assert len(items) == 1, "list must still return its rows even when enrich fails"
    assert "customer_name" not in items[0]
