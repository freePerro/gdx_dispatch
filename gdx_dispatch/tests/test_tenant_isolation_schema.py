"""Tenant isolation schema contract test.

pytest.mark: schema

Asserts that every ORM model with a ``company_id`` column has it declared as
``nullable=False``. This is Build Rule #5 from CLAUDE.md ("tenant isolation —
every query gets scoped") expressed at the schema layer.

WHY THIS TEST EXISTS
--------------------
On 2026-04-11 the drift_scanner flagged four models in
``gdx_dispatch/models/tenant_models.py`` with ``company_id nullable=True``:
ClientError, MobileSyncAction, User, Technician. An architecture review called
this "one query away from a data leak", and a production query that day found
one row in the live ``users`` table with ``company_id IS NULL`` (a
``qa-test@example.com`` artifact from QA work on 2026-04-08). The four were
pinned in ``KNOWN_XFAIL_NULLABLE_COMPANY_IDS`` until fixed.

All four have since been resolved: ClientError, User and Technician now declare
``company_id`` ``nullable=False``, and MobileSyncAction was removed as a model
with #739 (migration 095 drops ``mobile_sync_actions``). As of 2026-10-18 the
pinned set is empty and nothing is xfailed.

This is the SCHEMA-layer check only. The 2026-04-11 plan also named a DATA-layer
check (count NULL rows per tenant DB) at
``gdx_dispatch/tools/tenant_isolation_audit.py``; that tool was never committed
and no data-layer check exists (checked 2026-10-18).

HOW TO USE
----------
``.venv/bin/pytest gdx_dispatch/tests/test_tenant_isolation_schema.py -v``

Both tests pass with no xfails. Any model that adds ``company_id nullable=True``
fails ``test_all_company_id_columns_not_null``.

DO NOT fix this test by widening the xfail set. That is the ConsoleErrorTracker
anti-pattern from 2026-04-11 session_lessons.md: don't filter, fix.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.schema

# Import the ORM metadata so the test can walk every mapped class
from gdx_dispatch.models.tenant_models import Base

# Table names allowed to keep a nullable company_id. It held the four 2026-04-11
# violations until each was fixed or removed; it is empty and must stay empty.
# A new nullable company_id is fixed in the model, never added here.
KNOWN_XFAIL_NULLABLE_COMPANY_IDS: set[str] = set()


def _iter_company_id_columns():
    """Yield (tablename, column) for every mapped class with a company_id column."""
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        col = table.columns.get("company_id")
        if col is None:
            continue
        yield table.name, col


def test_all_company_id_columns_not_null() -> None:
    """Every ``company_id`` column on a tenant-scoped model must be ``nullable=False``.

    Build Rule #5: "company_id column always nullable=False — never nullable=True".

    If this test fails for a table NOT in KNOWN_XFAIL_NULLABLE_COMPANY_IDS, that
    is a regression — a new model shipped with a nullable tenant-scoping column,
    which is a data-leak footgun. Fix the model, don't widen the xfail set.
    """
    violations: list[str] = []
    xfailed: list[str] = []

    for tablename, col in _iter_company_id_columns():
        if col.nullable:
            if tablename in KNOWN_XFAIL_NULLABLE_COMPANY_IDS:
                xfailed.append(tablename)
            else:
                violations.append(
                    f"  {tablename}.company_id is nullable=True — "
                    f"tenant isolation schema violation (Build Rule #5)"
                )

    # Pinned tables (none since the set emptied) are reported, not failed.
    if xfailed:
        print(f"\n[xfail] {len(xfailed)} tables with pinned nullable=True "
              f"(pending migration): {sorted(xfailed)}")

    # Any NEW violation is a hard fail.
    if violations:
        pytest.fail(
            "\nNew tenant isolation schema violations (not in KNOWN_XFAIL list):\n"
            + "\n".join(violations)
            + "\n\nFix the model (set nullable=False). Do NOT add to the xfail set."
        )


def test_known_xfail_set_is_current() -> None:
    """Safety net: if the xfail set contains a table that is already fixed,
    flag it so we can remove the xfail entry and tighten the gate.

    This catches the drift where someone fixes a pinned violation but
    forgets to shrink the xfail set — which would silently hide regressions
    because the test would keep allowing nullable=True for that table name.
    """
    stale = []
    actual_nullable_tables = {
        tablename
        for tablename, col in _iter_company_id_columns()
        if col.nullable
    }
    for t in KNOWN_XFAIL_NULLABLE_COMPANY_IDS:
        if t not in actual_nullable_tables:
            stale.append(t)

    if stale:
        pytest.fail(
            f"KNOWN_XFAIL_NULLABLE_COMPANY_IDS contains tables that are "
            f"already nullable=False: {stale}. Remove them from the xfail "
            f"set in gdx_dispatch/tests/test_tenant_isolation_schema.py so the gate "
            f"stays tight."
        )
