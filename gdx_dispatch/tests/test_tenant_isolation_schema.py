"""Tenant isolation schema contract test.

pytest.mark: schema

Asserts that every ORM model with a ``company_id`` column has it declared as
``nullable=False``. This is Build Rule #5 from CLAUDE.md ("tenant isolation —
every query gets scoped") expressed at the schema layer.

WHY THIS TEST EXISTS
--------------------
On 2026-04-11, the drift_scanner flagged four tables in ``gdx_dispatch/models/tenant_models.py``
with ``company_id nullable=True`` — ClientError, MobileSyncAction, User, Technician.
Cerebras (architecture review via the an earlier session multi-agent coverage pass) called
this "one query away from a data leak." An immediate production query confirmed
1 actual row in the live GDX tenant's ``users`` table with ``company_id IS NULL``
(a ``qa-test@example.com`` artifact created 2026-04-08 during earlier QA work).

The four known violations are tracked via ``pytest.mark.xfail`` — they are NOT
hidden, they are pinned and visible in every test run, and they will flip to
passing automatically once the schema is fixed and the migration is landed.

This is the SCHEMA-layer check. The DATA-layer check (count NULL rows in every
tenant DB) lives at ``gdx_dispatch/tools/tenant_isolation_audit.py`` and is intended to
run as a cron/scheduled task. Schema enforcement and data enforcement are two
different failure modes and they need two different tests.

HOW TO USE
----------
``.venv/bin/pytest gdx_dispatch/tests/test_tenant_isolation_schema.py -v``

Expected output after 2026-04-11 an earlier session:
    - test_all_company_id_columns_not_null PASSED
        with 4 xfailed entries (ClientError, MobileSyncAction, User, Technician)

Once the schema migration is landed:
    1. Flip the four ``nullable=True`` attributes in ``gdx_dispatch/models/tenant_models.py``
       to ``nullable=False``
    2. Remove the xfail_ids set below
    3. This test should be 100% green, with no xfails
    4. Any NEW model that adds ``company_id nullable=True`` breaks this test loud

DO NOT fix this test by widening the xfail set. That is the ConsoleErrorTracker
anti-pattern from 2026-04-11 session_lessons.md: don't filter, fix.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.schema

# Import the ORM metadata so the test can walk every mapped class
from gdx_dispatch.models.tenant_models import Base

# Known violations pinned in place until the migration lands.
# Each entry is ``<tablename>.company_id``. Remove from this set once the
# corresponding row in tenant_models.py is changed to ``nullable=False``
# AND a matching Alembic migration has been run in production.
#
# See sprint 0.6 an earlier session discussion item "NULL company_id in production users
# table" — the migration is blocked on deciding what to do with the existing
# row that has ``company_id IS NULL``.
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

    # The 4 known-bad tables are expected failures — flag them loudly so
    # nobody forgets they exist, but don't fail the test on them.
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

    This catches the drift where someone fixes one of the four violations but
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
