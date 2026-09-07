"""The Job Costing "Cost breakdown" dialog: what it may call, and what is gone.

Every write control in that dialog was wired to an endpoint that does not exist.
Measured against the LIVE route table 2026-09-07, before the fix:

    GET    /api/jobs/{id}/parts            404 — no router has ever served it
    POST   /api/jobs/{id}/parts            404 — lived in the inventory module
                                           router deleted by #655; before that it
                                           took {part_id, qty_used} while the form
                                           sent {description, catalog_item_id,
                                           qty, unit_cost, unit_price}, so it 422'd
    PATCH  /api/jobs/{id}/parts/{part_id}  501 — a ui_compat `_not_implemented`
    DELETE /api/jobs/{id}/parts/{part_id}  405 — only PATCH was registered
    PATCH  /api/jobs/{id}/costing          405 — only GET is registered

So the panel never saved anything, in any verb, at any point. The `|| []` on the
read made a 404 look identical to "this job has no parts" — the failure was
silent as well as total.

The fix is not to build a fourth parts store. `job_parts_needed` is already the
billable spine for every capture path (routers/mobile.py says so, and
`_parts_for_job` costs it against confirmed vendor bill lines), with real CRUD at
`/api/jobs/{id}/parts-needed` and twenty frontend files using it. The dialog now
reports from `/api/costing/jobs/{id}` and links to the job for editing.

These tests pin the two halves that can regress: the routes the panel used are
gone and stay gone, and the endpoint it reports from still serves the fields the
panel renders.
"""

from __future__ import annotations

import pytest

# (method, path) for every route the dead panel called. None of these should
# exist. `POST /api/jobs/{job_id}/parts` is also covered by
# test_inventory_module_router_retired.py — deliberately duplicated, because the
# two removals had different causes and either could be reverted alone.
REMOVED = [
    ("GET", "/api/jobs/{job_id}/parts"),
    ("POST", "/api/jobs/{job_id}/parts"),
    ("PATCH", "/api/jobs/{job_id}/parts/{part_id}"),
    ("DELETE", "/api/jobs/{job_id}/parts/{part_id}"),
    ("PATCH", "/api/jobs/{job_id}/costing"),
]

# What the panel legitimately reads, and where editing actually lives. If one of
# these disappears the panel goes blank or its "edit on the job" link dead-ends,
# so they are asserted present rather than assumed.
REQUIRED = [
    ("GET", "/api/costing/jobs/{job_id}"),
    ("GET", "/api/jobs/{job_id}/line-items"),
    ("GET", "/api/jobs/{job_id}/parts-needed"),
    ("POST", "/api/jobs/{job_id}/parts-needed"),
]


@pytest.fixture(scope="module")
def registered() -> set[tuple[str, str]]:
    """Every (method, path) the app serves.

    Walks `include_router` wrappers via `conftest.iter_app_routes`. A plain
    `app.routes` walk is NOT usable: FastAPI defers `include_router`, so it sees
    a handful of entries and a guard written that way passes whatever the source
    says.
    """
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    return {
        (method, full_path)
        for full_path, route in iter_app_routes(app)
        for method in (getattr(route, "methods", None) or ())
    }


def test_the_walker_sees_a_real_route(registered: set[tuple[str, str]]) -> None:
    """Anti-vacuity: without this, an empty walk makes every check below pass."""
    assert ("GET", "/api/costing/jobs/{job_id}") in registered, (
        "route walk returned nothing usable; the assertions below prove nothing"
    )
    assert len(registered) > 500, f"only {len(registered)} routes walked"


@pytest.mark.parametrize(("method", "path"), REMOVED)
def test_dead_panel_route_is_gone(
    registered: set[tuple[str, str]], method: str, path: str
) -> None:
    assert (method, path) not in registered, (
        f"{method} {path} is registered again. Every verb the old Job Costing "
        "parts panel used was dead; re-adding one without a caller re-creates "
        "the orphan, and re-adding it WITH the old panel re-creates a fourth "
        "parts store competing with job_parts_needed."
    )


@pytest.mark.parametrize(("method", "path"), REQUIRED)
def test_the_surface_the_panel_actually_uses_still_exists(
    registered: set[tuple[str, str]], method: str, path: str
) -> None:
    assert (method, path) in registered, (
        f"{method} {path} is missing — the costing dialog reads from it, or its "
        "'edit parts on the job' link lands nowhere."
    )


def test_line_items_has_exactly_one_post_handler() -> None:
    """The ui_compat copy returned a fabricated success and wrote nothing.

    It never ran — sub_resources registers the same path first (app.py:1573 vs
    :1578) — but a duplicate whose loser fakes a 201 is one include-order change
    away from silently discarding every invoice line someone adds. `app.openapi()`
    collapses duplicates and names the LOSING handler, so this counts
    registrations directly instead.
    """
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    posts = [
        route
        for full_path, route in iter_app_routes(app)
        if full_path == "/api/jobs/{job_id}/line-items"
        and "POST" in (getattr(route, "methods", None) or ())
    ]
    assert len(posts) == 1, (
        f"{len(posts)} handlers registered for POST /api/jobs/{{job_id}}/line-items; "
        "a second one is a silent-write landmine"
    )
    assert getattr(posts[0], "endpoint", None).__module__.endswith("sub_resources"), (
        "the surviving handler must be the one that writes an InvoiceLine, not a shim"
    )


def test_costing_payload_carries_every_field_the_panel_renders(tmp_path) -> None:
    """The dialog stopped computing costs client-side and reads these instead.

    A real invocation against a real (empty) job, not a source grep: asserting
    `"catalog_variance" in inspect.getsource(...)` would pass for a comment. The
    failure this guards is a field renamed out from under the template, which is
    SILENT in Vue — `{{ jobDetail.catalog_variance }}` on a missing key renders
    empty rather than raising, so nothing else would notice.
    """
    import json
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from starlette.requests import Request

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
    from gdx_dispatch.routers.job_costing import get_job_costing

    engine = create_engine(
        f"sqlite:///{tmp_path / 'costing.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    try:
        resp = get_job_costing(job_id=uuid4(), request=req, _={}, db=db)
        payload = resp if isinstance(resp, dict) else json.loads(resp.body)
    finally:
        db.close()
        engine.dispose()

    for field in (
        "cost_incomplete",
        "unknown_cost_parts",
        "unlinked_bill_lines",
        "estimated_parts_cost",
        "catalog_variance",
        "total_cost",
        "margin_percent",
    ):
        assert field in payload, (
            f"/api/costing/jobs/{{job_id}} no longer returns {field}; the costing "
            "dialog renders it and would silently show blank"
        )
    # The Parts table iterates this. A rename to `rows` or a bare list would
    # leave the table permanently empty and look exactly like "no parts".
    assert isinstance(payload.get("parts"), dict), "parts must be an object"
    assert isinstance(payload["parts"].get("items"), list), (
        "the costing dialog renders parts.items; a shape change empties the table "
        "silently, which is the defect this whole change is about"
    )
