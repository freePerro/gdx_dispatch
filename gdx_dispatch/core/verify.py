"""verify_after_write — the structural guard against shape-1 silent failures.

WHY THIS EXISTS
---------------
an earlier session (2026-04-16) filed a retro saying ``UPDATE appointments SET
tech_id = technician_id WHERE tech_id IS NULL AND technician_id IS NOT NULL``
had updated 5 rows. 24 hours later (an earlier session, 2026-04-17) the live DB
showed 0 tech_id populated. The retro was a phantom success — Claude had
logged the UPDATE as committed based on internal state, not ground truth.

The pattern is bigger than that one retro. Across 20 sites surveyed for
SF-01 through SF-20 in ``silent_failure_registry.md``, shape 1 (reports
success without verifying outcome) accounted for ~14/20. The fix is
structural: every mutation operation that emits a "success" signal —
whether a log line, a retro entry, or a return value — must first
verify the expected post-condition against ground truth.

This module is that verification primitive.

USAGE
-----
    from gdx_dispatch.core.verify import verify_after_write, VerifyError

    with db.begin():
        rc = db.execute(
            text("UPDATE appointments SET tech_id = technician_id "
                 "WHERE tech_id IS NULL AND technician_id IS NOT NULL")
        ).rowcount
        verify_after_write(
            db,
            query="SELECT COUNT(tech_id) FROM appointments",
            expected=5,
            description="appointments.tech_id populated after backfill",
        )
        # If the SELECT returns anything other than 5, VerifyError raises
        # and the `with db.begin()` block rolls back. No phantom success.

For predicate checks (not just count equality):

    verify_after_write(
        db,
        query="SELECT COUNT(*) FROM appointments WHERE tech_id IS NULL",
        expected=lambda n: n == 0,
        description="no legacy tech_id NULL rows remain",
    )

For verifying against a snapshot of pre-state:

    pre_count = db.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
    # ... do work ...
    verify_after_write(
        db,
        query="SELECT COUNT(*) FROM jobs",
        expected=lambda n: n > pre_count,
        description="jobs row count strictly increased",
    )

CONTRACT
--------
- If expected is an int: query result MUST equal exactly.
- If expected is a callable: predicate MUST return truthy on the result.
- On mismatch: raises VerifyError with description + actual + expected.
- Never silently passes. Never logs-and-returns.

RELATIONSHIP TO TRANSACTIONS
-----------------------------
verify_after_write is meant to be called INSIDE an open transaction.
Raising inside ``with db.begin()`` triggers rollback automatically.
The helper does NOT manage its own transaction — that's the caller's
responsibility.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import text as sa_text


class VerifyError(AssertionError):
    """Raised when a post-write verification query does not match the expected
    state. Subclasses AssertionError so pytest surfaces it clearly.
    """

    def __init__(self, description: str, expected: Any, actual: Any):
        self.description = description
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"verify_after_write failed: {description} — "
            f"expected {expected!r}, got {actual!r}"
        )


def verify_after_write(
    db: Any,
    query: str,
    expected: int | Callable[[Any], bool],
    description: str,
    params: dict | None = None,
) -> Any:
    """Run ``query`` against ``db`` and assert the result matches ``expected``.

    Args:
        db: a SQLAlchemy Session / Connection / engine-bound executor with
            ``.execute()``.
        query: a SQL string (will be wrapped in ``sqlalchemy.text``).
        expected: either an int (equality check) or a callable
            ``f(actual) -> bool`` (predicate check).
        description: operator-facing description of what's being verified.
            Appears verbatim in the VerifyError message.
        params: optional bind parameters for the query.

    Returns:
        The query's scalar result (for chained assertions).

    Raises:
        VerifyError: when expected is an int and actual ≠ expected, OR
            when expected is a callable and the predicate returns falsy.
    """
    actual = db.execute(sa_text(query), params or {}).scalar()

    if callable(expected):
        if not expected(actual):
            raise VerifyError(description, expected="<predicate>", actual=actual)
    else:
        if actual != expected:
            raise VerifyError(description, expected=expected, actual=actual)

    return actual


def verify_all(
    db: Any, checks: list[tuple[str, Any, str]], params: dict | None = None,
) -> None:
    """Batch version — run multiple verification queries in sequence. First
    failure raises; remaining checks are not executed.

    Each check is ``(query, expected, description)``. ``expected`` follows
    the same rules as ``verify_after_write``.
    """
    for query, expected, description in checks:
        verify_after_write(db, query, expected, description, params=params)
