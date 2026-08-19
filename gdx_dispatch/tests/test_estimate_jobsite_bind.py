"""Conversion carries the jobsite (PR 3, jobsite-address plan).

A non-blank ``estimate.jobsite_address`` is an EXPLICIT different-address
answer; on accept it becomes a real ``customer_locations`` binding on the
new job. NULL/blank means "same as the customer" — nothing happens.
The bind is post-commit and guarded: failure degrades to an unbound job
with the address preserved in notes, never to no job at all. The public
token-holder gains no address write.
"""
# ruff: noqa: F811 — `client` is a pytest fixture imported from the proposals
# harness; every test's `client` parameter necessarily shadows the import.
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from gdx_dispatch.models.tenant_models import Customer
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


def _locations(client, customer_id):
    db = _db(client)
    try:
        return db.execute(
            text(
                "SELECT id, address, label, is_primary FROM customer_locations "
                "WHERE customer_id = :cid AND deleted_at IS NULL"
            ),
            {"cid": str(customer_id)},
        ).mappings().all()
    finally:
        db.close()


def _job_row(client, est_id):
    db = _db(client)
    try:
        est = db.get(Estimate, UUID(est_id))
        assert est.job_id is not None, "conversion did not create a job"
        # SQLite stores Uuid PKs undashed; match either form.
        row = db.execute(
            text("SELECT id, location_id, notes FROM jobs WHERE id IN (:a, :b)"),
            {"a": str(est.job_id), "b": est.job_id.hex},
        ).mappings().first()
        assert row is not None, "job row not found"
        return dict(row)
    finally:
        db.close()


def _accept(client, est_id, *, jobsite=None, customer_address=None, body=None, monkeypatch=None):
    if monkeypatch is not None:
        _fake_features(monkeypatch, deposit_pct=0)
    cust = _create_customer(client)
    if customer_address is not None:
        db = _db(client)
        try:
            c = db.get(Customer, UUID(cust))
            c.address = customer_address
            db.commit()
        finally:
            db.close()
    est = _create_estimate(client, customer_id=cust, jobsite_address=jobsite)
    _add_lines(client, est["id"], 1000.0)
    token = _publish(client, est["id"])
    r = client.post(f"/api/proposals/{token}/accept", json=body or {})
    assert r.status_code == 200, r.text
    return est["id"], cust


def test_different_jobsite_binds_a_created_location(client, monkeypatch):
    est_id, cust = _accept(
        client, None, jobsite="9 Dock Street, St Paul MN",
        customer_address="100 Billing Rd", monkeypatch=monkeypatch,
    )
    locs = _locations(client, cust)
    assert len(locs) == 1
    assert locs[0]["address"] == "9 Dock Street, St Paul MN"
    assert not locs[0]["is_primary"]  # inert for the customer's other jobs
    assert "Jobsite (" in (locs[0]["label"] or "")
    job = _job_row(client, est_id)
    assert str(job["location_id"]) == str(locs[0]["id"])


def test_blank_jobsite_means_same_as_customer_no_bind(client, monkeypatch):
    est_id, cust = _accept(
        client, None, jobsite=None,
        customer_address="100 Billing Rd", monkeypatch=monkeypatch,
    )
    assert _locations(client, cust) == []
    assert _job_row(client, est_id)["location_id"] is None


def test_typed_but_identical_address_skips_the_row(client, monkeypatch):
    est_id, cust = _accept(
        client, None, jobsite="100  billing rd.",
        customer_address="100 Billing Rd", monkeypatch=monkeypatch,
    )
    assert _locations(client, cust) == []
    assert _job_row(client, est_id)["location_id"] is None


def test_public_accept_body_cannot_inject_an_address(client, monkeypatch):
    """Trap 7: conversion reads ONLY the stored estimate field. An injected
    address in the accept payload must not become a location row or a bind
    — asserted behaviorally, not by schema construction."""
    est_id, cust = _accept(
        client, None, jobsite="9 Dock Street, St Paul MN",
        customer_address="100 Billing Rd", monkeypatch=monkeypatch,
        body={"jobsite_address": "666 Evil St", "address": "666 Evil St"},
    )
    locs = _locations(client, cust)
    assert len(locs) == 1
    assert locs[0]["address"] == "9 Dock Street, St Paul MN"
    assert all("Evil" not in (row["address"] or "") for row in locs)
    job = _job_row(client, est_id)
    assert str(job["location_id"]) == str(locs[0]["id"])


def test_same_address_twice_converges_on_one_row(client, monkeypatch):
    """Two estimates for the same customer at the same (normalized-variant)
    address share ONE location row — the find path, not a duplicate."""
    from gdx_dispatch.routers.estimates import _create_job_from_estimate

    _fake_features(monkeypatch, deposit_pct=0)
    cust = _create_customer(client)
    db = _db(client)
    try:
        c = db.get(Customer, UUID(cust))
        c.address = "100 Billing Rd"
        db.commit()
    finally:
        db.close()
    e1 = _create_estimate(client, customer_id=cust, jobsite_address="9 Dock Street")
    e2 = _create_estimate(client, customer_id=cust, jobsite_address="9  dock street.")
    db = _db(client)
    try:
        for eid in (e1["id"], e2["id"]):
            est = db.get(Estimate, UUID(eid))
            _create_job_from_estimate(est, db, "test-actor")
    finally:
        db.close()
    locs = _locations(client, cust)
    assert len(locs) == 1
    j1 = _job_row(client, e1["id"])
    j2 = _job_row(client, e2["id"])
    assert str(j1["location_id"]) == str(j2["location_id"]) == str(locs[0]["id"])


def test_bind_failure_never_sinks_the_convert_and_is_not_silent(client, monkeypatch):
    """The accept survives ANY bind failure — but the address the customer
    approved lands in the job's notes, never silently dropped."""
    import gdx_dispatch.core.job_site as job_site_mod

    def _boom(_value):
        raise RuntimeError("normalize exploded")

    monkeypatch.setattr(job_site_mod, "normalize_address", _boom)
    est_id, cust = _accept(
        client, None, jobsite="9 Dock Street, St Paul MN",
        customer_address="100 Billing Rd", monkeypatch=monkeypatch,
    )
    # Job exists (the accept survived) …
    job = _job_row(client, est_id)
    assert job["location_id"] is None
    # … and the sold address is preserved on it.
    assert "9 Dock Street, St Paul MN" in (job["notes"] or "")
