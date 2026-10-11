"""The office's job surfaces carry the jobsite's TOWN, not just the street
(GDXA-502).

A customer_locations row keeps city/state/zip in their own columns
(QuickBooks sync fills them apart from the street). core/job_site.py used to
select only the street, so job detail, the jobs list and the dispatch board
showed — and map-linked — "88 Pine Ridge Rd" with no town. Same HTTP harness
as test_job_visits_routes.py.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from gdx_dispatch.models.tenant_models import CustomerLocation
from gdx_dispatch.tests.test_job_visits_routes import _week, client  # noqa: F401
from gdx_dispatch.tests.test_visit_writers import DAY1, DAY3, T1, TENANT, _job, _visit, env  # noqa: F401

FULL = "88 Pine Ridge Rd, Anytown, MN 55555"


def _site(client, **kw):  # noqa: F811
    fields = {"label": "Home", "address": "88 Pine Ridge Rd", "city": "Anytown",
              "state": "MN", "zip": "55555"}
    fields.update(kw)
    loc = CustomerLocation(id=str(uuid4()), customer_id=str(client.cust.id),
                           company_id=TENANT, **fields)
    client.db.add(loc)
    client.db.commit()
    return loc


def test_job_detail_carries_the_town(client):  # noqa: F811
    loc = _site(client)
    job = _job(client, location_id=loc.id)
    body = client.client.get(f"/api/jobs/{job.id}").json()
    assert body["site_full_address"] == FULL
    # The street stays the street: the fix-address compare reads it.
    assert body["site_address"] == "88 Pine Ridge Rd"
    assert body["site_label"] == "Home"
    assert body["site_address_missing"] is False


def test_job_detail_primary_fallback_carries_the_town(client):  # noqa: F811
    _site(client, is_primary=True)
    job = _job(client)
    assert client.client.get(f"/api/jobs/{job.id}").json()["site_full_address"] == FULL


def test_job_detail_customer_fallback_is_the_customer_address(client):  # noqa: F811
    job = _job(client)
    body = client.client.get(f"/api/jobs/{job.id}").json()
    assert body["site_full_address"] == body["site_address"] == "11 Main"


def test_jobs_list_carries_the_town(client):  # noqa: F811
    loc = _site(client)
    job = _job(client, location_id=loc.id)
    items = client.client.get("/api/jobs").json()["items"]
    # Raw-SQL rows on SQLite carry the id as dashless hex.
    [row] = [r for r in items if UUID(str(r["id"])) == job.id]
    assert row["site_full_address"] == FULL
    assert row["site_address"] == "88 Pine Ridge Rd"


def test_dispatch_board_visit_card_carries_the_town(client):  # noqa: F811
    loc = _site(client)
    job = _job(client, crew=(T1,), location_id=loc.id)
    _visit(client, job, DAY1)
    _visit(client, job, DAY3)
    items = _week(client)["items"]
    assert items and {i["address"] for i in items} == {FULL}
    assert {i["site_full_address"] for i in items} == {FULL}
    assert {i["site_address"] for i in items} == {"88 Pine Ridge Rd"}
