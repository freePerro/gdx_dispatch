"""The review-request machinery is retired (2026-08-31) and must stay so.

`POST /api/reviews/request/{job_id}` recorded a "requested" review and sent
nothing (no UI called it); `routers/marketing.py` queued rows into
`review_requests`, a table with no reader (0 rows on prod and demo) — from a
function with no caller. Both are gone, with the ORM model and the table
(migration 085). The Reviews page keeps its list and stats.

Absence assertions on the live route table and the live metadata, not on
source text — a re-added route or model shows up here whatever file it
lives in.
"""
from __future__ import annotations

import importlib

from gdx_dispatch.tests.conftest import iter_app_routes


def _paths() -> set[str]:
    from gdx_dispatch.app import create_app

    return {path for path, _route in iter_app_routes(create_app())}


def test_no_review_request_route_survives():
    dead = sorted(p for p in _paths() if p.startswith("/api/reviews/request"))
    assert dead == [], f"review-request route is back: {dead}"


def test_reviews_list_and_stats_still_registered():
    paths = _paths()
    assert "/api/reviews" in paths
    assert "/api/reviews/stats" in paths


def test_review_request_model_and_table_are_gone():
    from gdx_dispatch import models as models_pkg
    from gdx_dispatch.core.audit import TenantBase

    assert not hasattr(models_pkg, "ReviewRequest")
    tenant_models = importlib.import_module("gdx_dispatch.models.tenant_models")
    assert not hasattr(tenant_models, "ReviewRequest")
    assert "review_requests" not in TenantBase.metadata.tables
    assert "customer_reviews" in TenantBase.metadata.tables, "the real review table must survive"


def test_marketing_no_longer_schedules_review_requests():
    marketing = importlib.import_module("gdx_dispatch.routers.marketing")
    assert not hasattr(marketing, "schedule_review_request_for_completed_job")
    assert not hasattr(marketing, "GOOGLE_REVIEWS_LINK")


def test_list_reviews_feeds_the_page_columns(tenant_db):
    """ReviewsView renders source / customer / content. The old payload had
    none of them, so the page showed blank Customer and Comment columns and
    every Source as "Unknown"."""
    import uuid

    from gdx_dispatch.models.tenant_models import Customer, CustomerReview
    from gdx_dispatch.routers.reviews import list_reviews

    cust = Customer(id=uuid.uuid4(), name="Page Customer", company_id="t-1")
    tenant_db.add(cust)
    tenant_db.add(CustomerReview(
        id="rv-1", tenant_id="t-1", company_id="t-1", customer_id=str(cust.id),
        rating=5, review_text="Great door", status="submitted", source="google",
        created_at="2026-08-31T00:00:00+00:00",
    ))
    tenant_db.commit()
    items = list_reviews(_={"user_id": "u"}, db=tenant_db)["items"]
    row = next(i for i in items if i["id"] == "rv-1")
    assert row["customer"] == "Page Customer"
    assert row["content"] == "Great door"
    assert row["source"] == "google"
    assert row["review_text"] == "Great door", "old key kept for any other reader"
