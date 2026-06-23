"""Regression brake — Sprint customer-multi-location (2026-05-21).

Pins the cross-router/cross-stack contract for binding a job to a specific
customer_locations row. Source-scan only — same style as
``test_dispatch_capacity_contract.py``. The full DB round-trip is covered
by the existing API tests in ``test_jobs_router.py``; this file's job is
to catch a refactor that drops the field on one side of the boundary.

Covers:
  • Job ORM declares location_id (varchar(36) FK to customer_locations.id)
  • JobCreate + JobUpdate accept location_id
  • create_job validates location belongs to customer (400 path)
  • update_job re-validates against the resolved customer_id
  • list_jobs SELECTs location_id + LEFT JOINs customer_locations
  • get_job exposes location_label + location_address
  • CustomerOut declares location_count
  • list_customers batches the COUNT (no N+1)
  • QB push_invoice prepends [label — address] to PrivateNote
  • the one-shot migration script declares ADD COLUMN + FK + index
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ─── ORM column declarations ─────────────────────────────────────────


def test_job_model_has_location_id_column():
    src = _read("gdx_dispatch/models/tenant_models.py")
    assert re.search(
        r"location_id:\s*Mapped\[str\s*\|\s*None\]\s*=\s*mapped_column\(\s*\n?\s*String\(36\)",
        src,
    ), "Job.location_id must be String(36) nullable"
    # Type must match customer_locations.id (also String(36)) — Postgres FK
    # source/target types must align. UUID-typed location_id would fail.
    assert 'ForeignKey("customer_locations.id")' in src, (
        "Job.location_id must declare ForeignKey('customer_locations.id')"
    )


# ─── Pydantic schemas ────────────────────────────────────────────────


def test_job_create_schema_accepts_location_id():
    src = _read("gdx_dispatch/routers/jobs.py")
    # Find the JobCreate class body and assert location_id is in it.
    create_match = re.search(
        r"class JobCreate\(BaseModel\):(.*?)class JobUpdate", src, re.DOTALL
    )
    assert create_match, "could not locate JobCreate class body"
    body = create_match.group(1)
    assert "location_id" in body, "JobCreate must declare location_id"
    assert "max_length=36" in body, "location_id must bound length to 36"


def test_job_update_schema_accepts_location_id():
    src = _read("gdx_dispatch/routers/jobs.py")
    update_match = re.search(
        r"class JobUpdate\(BaseModel\):(.*?)(?:\nclass |\ndef )", src, re.DOTALL
    )
    assert update_match, "could not locate JobUpdate class body"
    body = update_match.group(1)
    assert "location_id" in body


# ─── Router behavior (source-scan) ────────────────────────────────────


def test_create_job_validates_location_belongs_to_customer():
    src = _read("gdx_dispatch/routers/jobs.py")
    assert "_validate_location_for_customer" in src, (
        "create_job must validate location belongs to customer"
    )
    # The validator is called from create_job before the Job() instantiation.
    create_match = re.search(
        r"def create_job\(.*?(?=\n@router\.|\n(?:async )?def [a-zA-Z_])",
        src,
        re.DOTALL,
    )
    assert create_match
    create_body = create_match.group(0)
    assert "_validate_location_for_customer" in create_body, (
        "create_job body must call the validator"
    )
    assert "location_id=payload.location_id" in create_body, (
        "create_job must write location_id onto the Job()"
    )


def test_update_job_validates_location_against_resolved_customer():
    src = _read("gdx_dispatch/routers/jobs.py")
    update_match = re.search(
        r"def update_job\(.*?(?=\n@router\.|\n(?:async )?def [a-zA-Z_])",
        src,
        re.DOTALL,
    )
    assert update_match
    update_body = update_match.group(0)
    # location_id is mapped from the payload to the updates dict.
    assert 'updates["location_id"]' in update_body
    # The validator is invoked against updates.get("customer_id", job.customer_id).
    assert "_validate_location_for_customer" in update_body
    assert "updates.get(\"customer_id\"" in update_body, (
        "update_job must resolve customer_id from updates first, then fall "
        "back to the job's current customer — peer-customer attach guard"
    )


def test_list_jobs_joins_customer_locations():
    src = _read("gdx_dispatch/routers/jobs.py")
    # The page-fetch SELECT must include j.location_id plus the joined
    # location columns, and have a LEFT JOIN on customer_locations.
    assert "j.location_id" in src
    assert "cl.label AS location_label" in src
    assert "cl.address AS location_address" in src
    assert "LEFT JOIN customer_locations cl ON cl.id = j.location_id" in src


def test_get_job_exposes_location_label_and_address():
    src = _read("gdx_dispatch/routers/jobs.py")
    get_match = re.search(
        r"def get_job\(.*?(?=\n@router\.|\n(?:async )?def [a-zA-Z_])",
        src,
        re.DOTALL,
    )
    assert get_match
    get_body = get_match.group(0)
    # The detail endpoint loads the location row when job.location_id is set
    # and surfaces label + address on the dict.
    assert '"location_label"' in get_body
    assert '"location_address"' in get_body
    assert "SELECT label, address FROM customer_locations" in get_body


# ─── Customer list + count badge ─────────────────────────────────────


def test_customer_out_declares_location_count():
    src = _read("gdx_dispatch/routers/customers.py")
    out_match = re.search(
        r"class CustomerOut\(BaseModel\):(.*?)class CustomerListOut", src, re.DOTALL
    )
    assert out_match
    body = out_match.group(1)
    assert re.search(r"location_count:\s*int\s*=\s*0", body), (
        "CustomerOut must declare location_count: int = 0 (default for non-multi)"
    )


def test_list_customers_batches_location_count_query():
    src = _read("gdx_dispatch/routers/customers.py")
    # One COUNT GROUP BY for the whole page, not N+1 per customer.
    assert (
        "SELECT customer_id, COUNT(*) AS n FROM customer_locations" in src
    ), "list_customers must batch the location count, not query per row"
    # Customer rows in the response get the count merged in.
    assert "d[\"location_count\"] = location_counts.get(" in src


# ─── QB invoice memo prefix ──────────────────────────────────────────


def test_qb_push_invoice_prepends_location_to_memo():
    src = _read("gdx_dispatch/modules/quickbooks/sync.py")
    push_match = re.search(
        r"async def push_invoice\(.*?(?=\n@router\.|\n(?:async )?def [a-zA-Z_])",
        src,
        re.DOTALL,
    )
    assert push_match, "could not locate push_invoice body"
    body = push_match.group(0)
    # Memo is built from the linked job's customer_locations row.
    assert "FROM jobs j" in body
    assert "JOIN customer_locations cl" in body
    assert "j.location_id" in body
    # Prefix shape: "[label — address]" (em-dash). PrivateNote is the
    # back-end "internal memo" field — same field _pull_invoices reads.
    assert "PrivateNote" in body
    assert "[" in body and " — " in body


def test_qb_push_invoice_memo_is_idempotent():
    """Re-pushing must not double-bracket the memo.

    /audit catch 2026-05-21: the original code wrote a bracket-only
    PrivateNote, which on T3 pull-back into invoice.notes (local) +
    re-push would have produced "[Site — addr] [Site — addr]" forever.
    The body must strip an identical leading prefix before re-prepending.
    """
    src = _read("gdx_dispatch/modules/quickbooks/sync.py")
    push_match = re.search(
        r"async def push_invoice\(.*?(?=\n@router\.|\n(?:async )?def [a-zA-Z_])",
        src,
        re.DOTALL,
    )
    body = push_match.group(0)
    # The push must read invoice.notes (local body) and strip a leading
    # identical prefix before composing the new PrivateNote.
    assert "invoice.notes" in body, (
        "push_invoice must include the local invoice.notes body, not own "
        "the PrivateNote field exclusively (the QBO-admin-edit clobber risk "
        "is documented but the round-trip idempotency must hold)"
    )
    assert "startswith(prefix)" in body, (
        "push_invoice must strip an identical leading prefix to keep the "
        "push idempotent across round-trips"
    )


# NOTE: test_migration_script_declares_idempotent_alters_and_fk was removed —
# it read gdx_dispatch/tools/migrate_jobs_location_id.py, a one-off migration
# script folded into 001_squashed_baseline.py by the public-release squash. The
# location_id column/FK behavior it guarded is covered by the ORM/router tests
# above (test_job_model_has_location_id_column etc.).


def test_list_customers_count_query_is_sqlite_compatible():
    """/audit catch 2026-05-21 — ANY(:ids) is Postgres-only.

    test_customers_crud.py runs the customers router against a sqlite://
    fixture engine. The original ANY(:ids) form 500'd that suite. IN with
    an expanding bindparam works on both backends.
    """
    src = _read("gdx_dispatch/routers/customers.py")
    # The new location-count query must not use Postgres-only ANY().
    # (An unrelated pre-existing ANY() may exist in customers.py for a
    # different path — that's not in this sprint's scope.)
    assert "COUNT(*) AS n FROM customer_locations" in src, (
        "could not locate the sprint's location-count query"
    )
    # The expected IN-expanding form against customer_locations.
    assert "customer_id IN :ids" in src
    # The matching bindparam("ids", expanding=True) must be wired up.
    assert 'bindparam("ids", expanding=True)' in src
