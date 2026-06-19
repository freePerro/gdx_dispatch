"""Derive capability picker options from a FastAPI OpenAPI spec (SS-14 slice E).

# (frontend calls an endpoint that returns ``derive_capability_options(app.openapi())``).
# Wire that endpoint in gdx_dispatch/main.py or a dedicated router during integration.

Mapping rules (plain, source-of-truth-in-code):

    HTTP method → capability "action"
        GET / HEAD / OPTIONS          → "read"
        POST                          → "create"
        PUT / PATCH                   → "update"
        DELETE                        → "delete"

    URL path → capability "resource_type"
        The first path segment *after* a known API prefix is used.
        Known prefixes: "api", "v1", "v2", "public" (stripped in order).
        Trailing / leading slashes ignored. Parametrised segments like
        ``{id}`` are skipped so ``/api/jobs/{id}`` still yields "jobs".
        The resource is slugified (lowercase, ``-``/``_`` preserved).

Deduplication: the same (action, resource_type) pair surfaces once,
even when multiple endpoints share it (e.g. ``GET /api/jobs`` and
``GET /api/jobs/{id}`` both contribute ("read","jobs")).

Output shape — a list of option dicts, sorted by resource then action,
suitable for a Vue checkbox grid:

    [
        {"action": "read", "resource_type": "jobs",
         "label": "Read jobs", "paths": ["/api/jobs", "/api/jobs/{id}"]},
        ...
    ]

The function does NOT hit the network; callers pass in an already-
materialised OpenAPI dict (``app.openapi()``). This makes it trivially
testable without spinning up FastAPI.
"""
from __future__ import annotations

from typing import Any

_METHOD_TO_ACTION: dict[str, str] = {
    "get": "read",
    "head": "read",
    "options": "read",
    "post": "create",
    "put": "update",
    "patch": "update",
    "delete": "delete",
}

# Ordered: strip these as leading path segments until the first
# "real" resource segment is found. Ordering is not significant
# because we strip *all* matching prefixes before picking a segment.
_KNOWN_PREFIXES: frozenset[str] = frozenset({"api", "v1", "v2", "public"})


def _extract_resource(path: str) -> str | None:
    """Pick the resource-type segment from a URL path.

    Returns ``None`` if the path has no usable segment (e.g. ``/`` or
    ``/api/{anything}``).
    """
    if not path:
        return None
    segments = [seg for seg in path.strip("/").split("/") if seg]
    for seg in segments:
        low = seg.lower()
        if low in _KNOWN_PREFIXES:
            continue
        if seg.startswith("{") and seg.endswith("}"):
            continue
        return low
    return None


def derive_capability_options(openapi_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a de-duplicated, sorted list of capability picker options.

    ``openapi_spec`` is the dict returned by ``FastAPI.openapi()``.
    """
    paths = openapi_spec.get("paths") or {}
    if not isinstance(paths, dict):
        return []

    # key: (action, resource_type) → list of path strings
    bucket: dict[tuple[str, str], list[str]] = {}

    for path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        resource = _extract_resource(path)
        if resource is None:
            continue
        for method in operations:
            action = _METHOD_TO_ACTION.get(method.lower())
            if action is None:
                continue
            key = (action, resource)
            bucket.setdefault(key, []).append(path)

    options: list[dict[str, Any]] = []
    for (action, resource), path_list in bucket.items():
        options.append(
            {
                "action": action,
                "resource_type": resource,
                "label": f"{action.capitalize()} {resource}",
                "paths": sorted(set(path_list)),
            }
        )

    options.sort(key=lambda o: (o["resource_type"], o["action"]))
    return options
