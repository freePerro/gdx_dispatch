"""Tests for POST /api/estimates/{id}/duplicate.

Reuses the shared fixture in test_estimates.py — that file already builds a
FastAPI app with the estimates router, sqlite + module grants + tenant
middleware, so we import the `client` fixture directly via pytest's plugin
mechanism (re-declared here so test discovery doesn't depend on conftest).
"""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from gdx_dispatch.tests.test_estimates import client, _create_customer, _create_job, _create_estimate  # noqa: F401


def _add_line(client: TestClient, estimate_id: str, **overrides) -> dict:
    payload = {"description": "Spring", "quantity": 2, "unit_price": 120.50, "category": "Springs"}
    payload.update(overrides)
    r = client.post(f"/api/estimates/{estimate_id}/lines", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_duplicate_copies_header_fields_and_resets_state(client: TestClient):
    customer_id = _create_customer(client, name="Source Customer")
    src = _create_estimate(
        client,
        customer_id=customer_id,
        label="Shop doors r 10.29",
        notes="50% down required.",
        description="3 - 12x12 + 1 - 16x12",
    )
    # Add a line so the duplicate has something to copy + a non-zero total.
    _add_line(client, src["id"], description="12x12 door", quantity=1, unit_price=2181.71)

    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 201, r.text
    dup = r.json()

    # New identity
    assert dup["id"] != src["id"]
    UUID(dup["id"])  # well-formed UUID
    assert dup["estimate_number"] != src["estimate_number"]
    assert dup["estimate_number"].startswith("EST-")

    # Copied fields (label gets an incrementing "-N" suffix so option variants
    # of the same job stay distinguishable in lists).
    assert dup["customer_id"] == src["customer_id"]
    assert dup["label"] == "Shop doors r 10.29-1"
    assert dup["notes"] == "50% down required."
    assert dup["description"] == "3 - 12x12 + 1 - 16x12"

    # Reset state
    assert dup["status"] == "draft"
    assert dup["sent_at"] is None
    assert dup["accepted_at"] is None
    assert dup["declined_at"] is None
    assert dup["declined_reason"] is None

    # Duplicate starts unattached to a job
    assert dup["job_id"] is None


def test_duplicate_copies_lines_with_fresh_ids_and_recomputes_total(client: TestClient):
    src = _create_estimate(client, label="Multi-line source")
    _add_line(client, src["id"], description="12x12 door", quantity=1, unit_price=2181.71, category="Doors")
    _add_line(client, src["id"], description="Install labor", quantity=3, unit_price=900.00, category="Labor")

    src_full = client.get(f"/api/estimates/{src['id']}").json()
    src_line_ids = {ln["id"] for ln in src_full["lines"]}
    assert len(src_line_ids) == 2

    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 201
    dup = r.json()

    assert len(dup["lines"]) == 2
    dup_line_ids = {ln["id"] for ln in dup["lines"]}
    # All new line IDs
    assert dup_line_ids.isdisjoint(src_line_ids)

    # Same contents
    by_desc = {ln["description"]: ln for ln in dup["lines"]}
    assert by_desc["12x12 door"]["quantity"] == 1
    assert by_desc["12x12 door"]["unit_price"] == pytest.approx(2181.71)
    assert by_desc["12x12 door"]["category"] == "Doors"
    assert by_desc["Install labor"]["quantity"] == 3
    assert by_desc["Install labor"]["unit_price"] == pytest.approx(900.00)

    # Total = sum of line_totals
    expected_total = 1 * 2181.71 + 3 * 900.00
    assert dup["total"] == pytest.approx(expected_total)


def test_duplicate_mints_new_public_token(client: TestClient):
    """Source-estimate public_token is never returned to clients via the
    standard serializer (security — tokens are portal-access secrets), but
    we can verify the duplicate has its own row by checking the rows in DB.
    """
    from sqlalchemy import select

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.modules.proposals.models import Estimate

    src = _create_estimate(client, label="Token check source")
    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 201
    dup_id = r.json()["id"]

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        rows = db.execute(select(Estimate.id, Estimate.public_token).where(Estimate.id.in_([UUID(src["id"]), UUID(dup_id)]))).all()
        tokens = {str(rid): tok for rid, tok in rows}
    finally:
        db.close()

    assert tokens[src["id"]] != tokens[dup_id]
    assert len(tokens[dup_id]) > 20  # secrets.token_urlsafe(48)[:64]


def test_duplicate_404_for_missing_estimate(client: TestClient):
    from uuid import uuid4

    r = client.post(f"/api/estimates/{uuid4()}/duplicate")
    assert r.status_code == 404


def test_duplicate_404_for_soft_deleted_source(client: TestClient):
    src = _create_estimate(client, label="To be deleted")
    d = client.delete(f"/api/estimates/{src['id']}")
    assert d.status_code in (200, 204)

    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 404


def test_duplicate_of_realistic_accepted_source_resets_full_state(client: TestClient):
    """Accepted estimates carry not just status='accepted' but accepted_at,
    accepted_tier_id, signed_at, signed_by, signature_data, sent_at,
    reminder_sent_at, valid_until. The duplicate must reset all of them —
    auditor 2026-05-27 flagged the prior test as theater for only flipping
    the status string.
    """
    from datetime import datetime, timezone
    from uuid import UUID as _UUID

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.modules.proposals.models import Estimate

    src = _create_estimate(client, label="Won deal — clone for next one")
    _add_line(client, src["id"], description="Door", quantity=1, unit_price=1000.0)

    now = datetime.now(timezone.utc)
    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.get(Estimate, _UUID(src["id"]))
        row.status = "accepted"
        row.sent_at = now
        row.accepted_at = now
        row.signed_at = now
        row.signed_by = "Jane Customer"
        row.signature_data = "data:image/png;base64,iVBORw0KGgo="
        row.reminder_sent_at = now
        row.valid_until = now
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 201, r.text
    dup = r.json()

    assert dup["status"] == "draft"
    assert dup["sent_at"] is None
    assert dup["accepted_at"] is None
    assert dup["declined_at"] is None
    # signed_* live on the row but aren't in _serialize_estimate; verify via DB.
    db = next(dep())
    try:
        new_row = db.get(Estimate, _UUID(dup["id"]))
        assert new_row.signed_at is None
        assert new_row.signed_by is None
        assert new_row.signature_data is None
        assert new_row.reminder_sent_at is None
        assert new_row.valid_until is None
        assert new_row.accepted_tier_id is None
    finally:
        db.close()

    assert len(dup["lines"]) == 1


def test_duplicate_proposal_mode_clones_tiers(client: TestClient):
    """proposal_mode=True + zero tiers = a structurally broken proposal that
    renders an empty good/better/best picker on /mobile/quoting. Auditor
    2026-05-27 flagged this as the foundational lie. Verify tier rows are
    carbon-copied with fresh ids and fresh estimate_id.
    """
    from uuid import UUID as _UUID

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.modules.proposals.models import Estimate, ProposalTier

    src = _create_estimate(client, label="Tiered proposal source")
    _add_line(client, src["id"], description="Base door package", quantity=1, unit_price=2500.0)

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.get(Estimate, _UUID(src["id"]))
        row.proposal_mode = True
        db.add(ProposalTier(estimate_id=row.id, tier_name="good", description="Steel insulated", total_price=2500.0, includes_parts=True, warranty_months=12, display_order=1))
        db.add(ProposalTier(estimate_id=row.id, tier_name="better", description="Aluminum carriage-house", total_price=3800.0, includes_parts=True, warranty_months=24, display_order=2))
        db.add(ProposalTier(estimate_id=row.id, tier_name="best", description="Full insulated + smart opener", total_price=5200.0, includes_parts=True, warranty_months=60, display_order=3))
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 201, r.text
    dup = r.json()

    db = next(dep())
    try:
        src_tier_ids = {t.id for t in db.execute(
            select(ProposalTier).where(ProposalTier.estimate_id == _UUID(src["id"]))
        ).scalars().all()}
        new_tiers = db.execute(
            select(ProposalTier).where(ProposalTier.estimate_id == _UUID(dup["id"]))
        ).scalars().all()

        assert len(new_tiers) == 3
        assert {t.id for t in new_tiers}.isdisjoint(src_tier_ids)
        by_name = {t.tier_name: t for t in new_tiers}
        assert pytest.approx(float(by_name["good"].total_price)) == 2500.0
        assert by_name["better"].warranty_months == 24
        assert by_name["best"].display_order == 3
        assert all(t.stripe_payment_link is None for t in new_tiers)
    finally:
        db.close()


def test_duplicate_label_appends_incrementing_suffix(client: TestClient):
    """Duplicating for door-option variants must yield distinguishable job
    names. First dup of "Job" -> "Job-1"; duplicating "Job" again -> "Job-2"
    (lowest free N, no collision); duplicating "Job-1" -> "Job-2" as well
    (increments the shared base, never stacks "-1-1").
    """
    src = _create_estimate(client, label="123 Main St - Door Replacement")

    dup1 = client.post(f"/api/estimates/{src['id']}/duplicate").json()
    assert dup1["label"] == "123 Main St - Door Replacement-1"

    # Re-duplicating the ORIGINAL should not collide with dup1.
    dup2 = client.post(f"/api/estimates/{src['id']}/duplicate").json()
    assert dup2["label"] == "123 Main St - Door Replacement-2"

    # Duplicating an already-suffixed estimate increments the base, no "-1-1".
    dup3 = client.post(f"/api/estimates/{dup1['id']}/duplicate").json()
    assert dup3["label"] == "123 Main St - Door Replacement-3"


def test_duplicate_label_handles_null_and_blank(client: TestClient):
    """A null/blank job name stays null/blank on the duplicate — we only
    suffix when there's an actual name to distinguish.
    """
    src = _create_estimate(client, label=None)
    dup = client.post(f"/api/estimates/{src['id']}/duplicate").json()
    assert dup["label"] in (None, "")


def test_duplicate_drops_soft_deleted_customer(client: TestClient):
    """If the original customer was merged/soft-deleted between source-create
    and duplicate, mirror the create path: drop the customer_id rather than
    leave a dangling FK on a draft. User can re-pick in the UI.
    """
    from uuid import UUID as _UUID

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.models.tenant_models import Customer

    customer_id = _create_customer(client, name="Will Be Deleted")
    src = _create_estimate(client, customer_id=customer_id, label="Pre-merge estimate")
    _add_line(client, src["id"], description="Door", quantity=1, unit_price=900.0)

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        cust = db.get(Customer, _UUID(customer_id))
        from datetime import datetime, timezone
        cust.deleted_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 201, r.text
    dup = r.json()

    assert dup["customer_id"] is None
    assert len(dup["lines"]) == 1  # lines still copied; user just needs to re-pick the customer
