"""`modules/inventory/router.py` is gone: four unauthenticated routes, none of which worked.

The router carried only `Depends(require_module("inventory"))`. That gates on the
tenant's module list; it does **not** authenticate (`core/modules.py`). All four
routes were therefore reachable anonymously, two of them writing money-adjacent
data: `POST /inventory/parts/{id}/adjust` moved stock, and
`PUT /inventory/parts/{id}` could rewrite `unit_cost` and `unit_price`.

Measured on prod 2026-09-07, before the removal:
`GET /api/inventory/parts/<uuid>/stock` answered **404** — it reached the handler
and the row was missing — while the authenticated `GET /api/inventory/items`
answered **401**.

Nothing was exposed in practice, and the reason is why these were deleted rather
than gated: **the `parts` catalog has no writer anywhere in the app**, so every
one of these handlers dead-ended on a lookup that could not succeed. The one
route with an apparent caller, `POST /api/jobs/{job_id}/parts`, was called by the
Job Costing "Add Part" dialog with `{description, catalog_item_id, qty,
unit_cost, unit_price}` against a body model requiring `part_id: UUID` — a 422 on
every call it ever made. That screen is dead in all four verbs; see #653.

`tests/authz_sweep.py` cannot catch this class: it counts any authenticated route
as gated, so a route with no auth dependency at all never enters its view.
"""
from __future__ import annotations

import importlib.util

import pytest

# (method, path) for every route the deleted router registered.
REMOVED = [
    ("PUT", "/api/inventory/parts/{part_id}"),
    ("GET", "/api/inventory/parts/{part_id}/stock"),
    ("POST", "/api/inventory/parts/{part_id}/adjust"),
    ("POST", "/api/jobs/{job_id}/parts"),
]


@pytest.fixture(scope="module")
def registered() -> set[tuple[str, str]]:
    """Every (method, path) the app serves.

    Walks `include_router` wrappers via `conftest.iter_app_routes`. A plain
    `app.routes` walk is NOT usable: FastAPI defers `include_router`, so it sees
    a handful of entries and no `/api/inventory` path at all — a guard written
    that way passes whatever the source says.
    """
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    return {
        (method, full_path)
        for full_path, route in iter_app_routes(app)
        for method in (getattr(route, "methods", None) or ())
    }


def test_the_walker_sees_a_real_route(registered: set[tuple[str, str]]) -> None:
    """Anti-vacuity: without this, an empty walk would make every check below pass."""
    assert ("GET", "/api/inventory/items") in registered, (
        "the route walker found nothing — the assertions in this file would be vacuous"
    )


@pytest.mark.parametrize("method,path", REMOVED)
def test_removed_route_is_not_registered(
    registered: set[tuple[str, str]], method: str, path: str
) -> None:
    assert (method, path) not in registered, (
        f"{method} {path} is registered again. It was unauthenticated and its table "
        "has no writer; routers/inventory.py serves the live inventory surface."
    )


def test_the_module_no_longer_ships_a_router() -> None:
    """The file is gone and the package no longer re-exports it.

    `modules/inventory/__init__.py` used to `from ... import router`, so deleting
    the file alone broke every importer of `modules.inventory.stock` — which is
    `routers/inventory.py`, `routers/purchase_orders.py` and
    `modules/vendor_invoices/confirm.py`. That silently unmounted 23 routes while
    the app still imported cleanly.
    """
    assert importlib.util.find_spec("gdx_dispatch.modules.inventory.router") is None

    from gdx_dispatch.modules import inventory

    assert "router" not in inventory.__all__
    assert importlib.util.find_spec("gdx_dispatch.modules.inventory.stock") is not None


def test_the_live_inventory_surface_survived(registered: set[tuple[str, str]]) -> None:
    """The regression the deletion nearly caused: these come from importers of
    `modules.inventory.stock` and vanish if that package fails to import."""
    for method, path in (
        ("GET", "/api/inventory/items"),
        ("POST", "/api/inventory/items/{item_id}/adjust"),
        ("GET", "/api/purchase-orders"),
        ("GET", "/api/vendor-invoices"),
    ):
        assert (method, path) in registered, f"{method} {path} disappeared"
