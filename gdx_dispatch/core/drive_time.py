"""Drive-time estimates between today's-route stops.

Sprint tech_mobile S1-A2.

Per-tenant configurability (catalog key ``tech_mobile.drive_time_provider``):

- ``google`` — Google Distance Matrix API (default). Server-side; results
  cached in Redis for 15 min keyed by (tenant_id, address tuple) so a tech
  loading /today multiple times in a row doesn't burn API quota.
- ``mapbox`` — Reserved. Returns the same result shape as ``off`` until a
  Mapbox client lands; a single warning per request is logged so it's
  obvious the setting is "planned, not active".
- ``off`` — No real-time drive-time. Cards display only the appointment's
  scheduled_at; the frontend simply omits the ETA badge.

Public API:
    compute_drive_times(tenant_id, addresses, *, provider) -> list[int | None]

Returns a list aligned with ``addresses``: index 0 is always ``None`` (no
leg before the first stop); index i ≥ 1 is the drive-time in **seconds**
from addresses[i-1] → addresses[i], or ``None`` if the API couldn't
resolve that leg.

The function is async because the cache helper is async; the underlying
Google client call is sync (offloaded to a thread via ``asyncio.to_thread``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from gdx_dispatch.core.cache import cached


log = logging.getLogger(__name__)


PROVIDER_GOOGLE = "google"
PROVIDER_MAPBOX = "mapbox"
PROVIDER_OFF = "off"
_DRIVE_TIME_TTL_SEC = 900  # 15 minutes


def _route_cache_key(addresses: list[str], provider: str) -> str:
    payload = json.dumps([provider, addresses], separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"drive_time:{provider}:{digest}"


def _empty_legs(addresses: list[str]) -> list[int | None]:
    return [None] * len(addresses)


def _google_distance_matrix(addresses: list[str]) -> list[int | None]:
    """Sync call to Google Distance Matrix. Returns aligned leg list.

    One matrix call with origins=addresses[:-1] and destinations=addresses[1:].
    Reads the diagonal so we get exactly the sequential legs we need rather
    than the full N x N grid. Distance Matrix supports up to 25 origins +
    25 destinations per call; tech routes are typically ≤ 12 stops, well
    under the limit.
    """
    if len(addresses) < 2:
        return _empty_legs(addresses)
    try:
        from gdx_dispatch.routers.maps import get_google_maps_client

        client = get_google_maps_client()
    except Exception:
        log.exception("drive_time_client_init_failed")
        return _empty_legs(addresses)

    origins = addresses[:-1]
    destinations = addresses[1:]
    try:
        matrix = client.distance_matrix(origins=origins, destinations=destinations)
    except Exception:
        log.exception("drive_time_distance_matrix_failed")
        return _empty_legs(addresses)

    legs: list[int | None] = [None]
    rows = matrix.get("rows") or []
    for i in range(len(origins)):
        leg: int | None = None
        if i < len(rows):
            elements = rows[i].get("elements") or []
            if i < len(elements):
                el = elements[i]
                if el.get("status") == "OK":
                    duration = el.get("duration") or {}
                    val = duration.get("value")
                    if isinstance(val, (int, float)):
                        leg = int(val)
        legs.append(leg)
    return legs


async def compute_drive_times(
    tenant_id: str,
    addresses: list[str],
    *,
    provider: str = PROVIDER_GOOGLE,
) -> list[int | None]:
    """Resolve sequential drive-times between ``addresses``.

    Provider behavior:
      - ``off``: skip; return all-None.
      - ``mapbox``: not implemented yet; return all-None and log once.
      - ``google``: cached call to Distance Matrix.

    Empty / single-stop input always returns the matching all-None list
    so callers can zip the result against their card list without a
    branch.
    """
    if not addresses:
        return []
    if len(addresses) < 2:
        return _empty_legs(addresses)

    # Drop blanks: Google rejects empty strings; if any address is blank,
    # we fall back to all-None so a single missing address doesn't sink
    # the whole route.
    if any(not (a or "").strip() for a in addresses):
        return _empty_legs(addresses)

    if provider == PROVIDER_OFF:
        return _empty_legs(addresses)

    if provider == PROVIDER_MAPBOX:
        log.info("drive_time_provider_mapbox_not_implemented_yet tenant=%s", tenant_id)
        return _empty_legs(addresses)

    if provider != PROVIDER_GOOGLE:
        log.warning("drive_time_unknown_provider tenant=%s provider=%s", tenant_id, provider)
        return _empty_legs(addresses)

    cache_key = _route_cache_key(addresses, provider)

    def _fetcher() -> Any:
        return _google_distance_matrix(addresses)

    try:
        # cached() awaits the fetcher if it returns an awaitable; the sync
        # _fetcher returns a list directly. Wrap in asyncio.to_thread so a
        # slow Google response doesn't block the event loop.
        async def _async_fetch() -> Any:
            return await asyncio.to_thread(_fetcher)

        return await cached(tenant_id, cache_key, _DRIVE_TIME_TTL_SEC, _async_fetch)
    except Exception:
        log.exception("drive_time_compute_failed tenant=%s", tenant_id)
        return _empty_legs(addresses)
