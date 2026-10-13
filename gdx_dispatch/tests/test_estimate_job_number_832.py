"""A converted estimate yields a numbered job (GDXA-832).

Every estimate→job path (staff accept, manual convert-to-job, the public
proposal accept, the portal accept) goes through
``_create_job_from_estimate``; it never assigned ``job_number``, so prod held
converted jobs with no number. The helper now takes the next number for the
tenant in estimate.company_id, the way the desktop create does. The allocator
is faked because it locks with FOR UPDATE, which SQLite rejects; its own
behaviour is pinned in test_job_number_uniqueness.
"""
# ruff: noqa: F811 — `client` is a pytest fixture imported from the proposals
# harness; every test's `client` parameter necessarily shadows the import.
from __future__ import annotations

from uuid import UUID

import pytest

from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.modules.proposals.models import Estimate

# Reuse the proposals harness wholesale — same app, same routes, same seeding.
from gdx_dispatch.tests.test_proposals import (  # noqa: F401
    TENANT,
    _add_lines,
    _create_customer,
    _create_estimate,
    _db,
    _fake_features,
    _publish,
    client,
)


@pytest.fixture()
def numbering(monkeypatch):
    """The real allocate_job_number around a fake counter; records each call."""
    import gdx_dispatch.core.database as database
    import gdx_dispatch.modules.numbering.service as service

    calls: list[tuple[str, str | None]] = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

    def fake_next(cdb, tenant_id, *, customer_name=None):
        calls.append((str(tenant_id), customer_name))
        return f"JOB-T-{len(calls):03d}"

    monkeypatch.setattr(database, "SessionLocal", _Session)
    monkeypatch.setattr(service, "next_job_number", fake_next)
    _fake_features(monkeypatch, deposit_pct=0)
    return calls


def _estimate(client, name="Sample Customer"):
    cust = _create_customer(client, name=name)
    est = _create_estimate(client, customer_id=cust)
    _add_lines(client, est["id"], 1000.0)
    return est["id"]


def _job_number(client, est_id):
    db = _db(client)
    try:
        est = db.get(Estimate, UUID(est_id))
        assert est.job_id is not None, "conversion did not create a job"
        return db.get(Job, est.job_id).job_number
    finally:
        db.close()


def test_staff_accept_numbers_the_job(client, numbering):
    est_id = _estimate(client)
    _publish(client, est_id)
    r = client.post(f"/api/estimates/{est_id}/accept", json={})
    assert r.status_code == 200, r.text
    assert _job_number(client, est_id) == "JOB-T-001"
    assert numbering == [(TENANT, "Sample Customer")]


def test_public_proposal_accept_numbers_the_job(client, numbering):
    est_id = _estimate(client)
    token = _publish(client, est_id)
    r = client.post(f"/api/proposals/{token}/accept", json={})
    assert r.status_code == 200, r.text
    assert _job_number(client, est_id) == "JOB-T-001"
    assert numbering == [(TENANT, "Sample Customer")]


def test_manual_convert_numbers_the_job(client, numbering):
    est_id = _estimate(client)
    _publish(client, est_id, status="accepted")
    r = client.post(f"/api/estimates/{est_id}/convert-to-job")
    assert r.status_code == 200, r.text
    assert _job_number(client, est_id) == "JOB-T-001"
    assert numbering == [(TENANT, "Sample Customer")]


def test_the_helper_numbers_the_job_for_any_caller(client, numbering):
    """The portal accept (routers/portal.py) calls the helper directly; the
    tenant comes from estimate.company_id, not from the caller."""
    from gdx_dispatch.routers.estimates import _create_job_from_estimate

    est_id = _estimate(client)
    db = _db(client)
    try:
        est = db.get(Estimate, UUID(est_id))
        job = _create_job_from_estimate(est, db, "user-1")
        assert job.job_number == "JOB-T-001"
    finally:
        db.close()
    assert numbering == [(TENANT, "Sample Customer")]


def test_a_numbering_failure_leaves_the_job_unnumbered_not_unmade(client, numbering, monkeypatch):
    import gdx_dispatch.modules.numbering.service as service

    def boom(*_a, **_k):
        raise RuntimeError("counter unavailable")

    monkeypatch.setattr(service, "next_job_number", boom)
    est_id = _estimate(client)
    _publish(client, est_id)
    r = client.post(f"/api/estimates/{est_id}/accept", json={})
    assert r.status_code == 200, r.text
    assert r.json().get("job_id") or r.json().get("auto_converted_job_id")
    assert _job_number(client, est_id) is None
