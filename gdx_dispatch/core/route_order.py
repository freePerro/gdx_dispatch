"""Universal FastAPI route reordering — literal segments beat parameterized.

FastAPI matches routes in registration order. When a router declares
`/{customer_id}` before `/duplicates`, a request to `/duplicates` is captured
by the parameterized route and the handler runs with `customer_id="duplicates"`
— typically 404 or 500 (or worse, a DataError if the column is UUID).

This helper applies a stable reorder: routes whose first differing segment
(compared to other routes in the same prefix family) is literal are bubbled
ahead of routes whose differing segment is a path parameter.

Apply once, after all routers are registered on the app, in app.py.
"""
from __future__ import annotations

from typing import Any


def _path_specificity_key(path: str) -> tuple:
    """Lower tuples sort earlier. Literal segments beat `{param}` segments."""
    segs = path.strip("/").split("/")
    # For each segment, 0 if literal, 1 if parameterized. Then length.
    return tuple(1 if s.startswith("{") else 0 for s in segs) + (len(segs),)


def reorder_literal_paths_first(app_or_router: Any) -> int:
    """Stable-sort app.routes so literal-segment paths match before param ones.

    Returns the number of routes that moved from their original position.

    FastAPI's `app.routes` is a read-only property; the mutable list lives on
    `app.router.routes`. On APIRouter, `.routes` is directly mutable. Handle
    both by locating the underlying list.
    """
    # Find the mutable list
    backing = getattr(app_or_router, "router", app_or_router)
    routes_list = backing.routes  # list on APIRouter
    original_order = {id(r): i for i, r in enumerate(routes_list)}

    def key(r):
        path = getattr(r, "path", "")
        return _path_specificity_key(path) + (original_order[id(r)],)

    # Mutate in place so any references to the list stay valid.
    sorted_routes = sorted(routes_list, key=key)
    moved = sum(1 for i, r in enumerate(sorted_routes) if original_order[id(r)] != i)
    routes_list[:] = sorted_routes
    return moved
