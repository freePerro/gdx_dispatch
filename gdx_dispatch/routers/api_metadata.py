"""SS-25 Slice E — ``/api/meta/*`` metadata endpoints.

Public read-only surface. Two endpoints:

* ``GET /api/meta/versions`` — supported + deprecated GDX API versions
  and the current "latest" default. Clients use this to decide which
  vendor media type to pin in their ``Accept`` header.
* ``GET /api/meta/deprecations`` — full deprecation registry. A
  machine-readable companion to the human-facing
  ``docs/deprecation-policy.md``. Consumers can poll this to build
  dashboards / alerts ahead of sunset.

Both endpoints are unauthenticated by design — the metadata is
public. Nothing returned here is tenant-scoped.

Mounted via the ``_ss_routers`` loop in ``gdx_dispatch/app.py``.
"""
from __future__ import annotations

from fastapi import APIRouter

from gdx_dispatch.core.api_version import SUPPORTED_VERSIONS, latest_version
from gdx_dispatch.core.deprecation_registry import DeprecationRegistry, get_registry

router = APIRouter(prefix="/api/meta", tags=["meta"])


def _registry() -> DeprecationRegistry:
    # Indirection so tests can monkeypatch ``get_registry`` in
    # gdx_dispatch.core.deprecation_registry without re-importing this module.
    return get_registry()


@router.get("/versions")
def list_versions() -> dict[str, object]:
    """List supported API versions and the current default.

    Response shape::

        {
            "supported": [1],
            "latest": 1,
            "media_type_template": "application/vnd.gdx.v<N>+json"
        }
    """
    return {
        "supported": list(SUPPORTED_VERSIONS),
        "latest": latest_version(),
        "media_type_template": "application/vnd.gdx.v<N>+json",
    }


@router.get("/deprecations")
def list_deprecations() -> dict[str, object]:
    """Return the full deprecation registry for this deployment.

    Response shape::

        {
            "count": 2,
            "deprecations": [
                {
                    "endpoint": "/api/v1/customers",
                    "deprecated_at": "2026-04-01T00:00:00+00:00",
                    "sunset_at":     "2028-04-01T00:00:00+00:00",
                    "replacement_endpoint": "/api/v2/customers"
                },
                ...
            ]
        }
    """
    entries = _registry().all_entries()
    return {
        "count": len(entries),
        "deprecations": [e.to_public_dict() for e in entries],
    }
