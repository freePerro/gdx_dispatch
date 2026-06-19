from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gdx_dispatch.routers import maps as maps_router
from gdx_dispatch.routers.auth import get_current_user


class FakeGoogleMapsClient:
    def __init__(self, *, geocode_result=None, directions_result=None, matrix_result=None):
        self._geocode_result = geocode_result if geocode_result is not None else []
        self._directions_result = directions_result if directions_result is not None else []
        self._matrix_result = matrix_result if matrix_result is not None else {}

    def geocode(self, address: str):
        return self._geocode_result

    def reverse_geocode(self, latlng):
        return self._geocode_result

    def directions(self, origin, destination, waypoints=None, optimize_waypoints=False):
        return self._directions_result

    def distance_matrix(self, origins, destinations):
        return self._matrix_result


def _request_with_tenant(tenant: dict | None = None) -> object:
    return SimpleNamespace(state=SimpleNamespace(tenant=tenant or {}))


def test_geocode_returns_coords(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(
        maps_router,
        "get_google_maps_client",
        lambda: FakeGoogleMapsClient(
            geocode_result=[
                {
                    "geometry": {"location": {"lat": 41.881832, "lng": -87.623177}},
                    "formatted_address": "Chicago, IL, USA",
                }
            ]
        ),
    )

    body = maps_router.geocode_address(maps_router.GeocodeIn(address="Chicago"))
    assert body["lat"] == 41.881832
    assert body["lng"] == -87.623177


def test_geocode_not_found_returns_404(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(
        maps_router,
        "get_google_maps_client",
        lambda: FakeGoogleMapsClient(geocode_result=[]),
    )

    with pytest.raises(HTTPException) as exc:
        maps_router.geocode_address(maps_router.GeocodeIn(address="nowhere"))
    assert exc.value.status_code == 404


def test_optimize_route_returns_ordered_ids(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(
        maps_router,
        "get_google_maps_client",
        lambda: FakeGoogleMapsClient(
            directions_result=[
                {
                    "waypoint_order": [1, 0],
                    "legs": [{"duration": {"value": 1200}, "distance": {"value": 16000}}],
                }
            ]
        ),
    )

    payload = maps_router.OptimizeRouteIn(
        tech_start_location="Depot",
        jobs=[
            maps_router.RouteJobIn(id="job-1", address="111 First St"),
            maps_router.RouteJobIn(id="job-2", address="222 Second St"),
        ],
    )
    body = maps_router.optimize_route(payload)
    assert body["optimized_job_ids"] == ["job-2", "job-1"]


def test_drive_time_returns_duration(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(
        maps_router,
        "get_google_maps_client",
        lambda: FakeGoogleMapsClient(
            matrix_result={
                "rows": [
                    {
                        "elements": [
                            {
                                "status": "OK",
                                "duration": {"text": "15 mins", "value": 900},
                                "distance": {"text": "8 mi", "value": 12874},
                            }
                        ]
                    }
                ]
            }
        ),
    )

    body = maps_router.drive_time(maps_router.DriveTimeIn(origin="A", destination="B"))
    assert body["duration_seconds"] == 900
    assert body["distance_meters"] == 12874


def test_service_area_inside_returns_true(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(
        maps_router,
        "get_google_maps_client",
        lambda: FakeGoogleMapsClient(
            geocode_result=[
                {
                    "geometry": {"location": {"lat": 41.88, "lng": -87.62}},
                    "formatted_address": "Inside Address",
                }
            ]
        ),
    )

    polygon = [
        [-87.70, 41.80],
        [-87.50, 41.80],
        [-87.50, 41.95],
        [-87.70, 41.95],
        [-87.70, 41.80],
    ]
    payload = maps_router.ServiceAreaCheckIn(address="Inside", service_polygon=polygon)
    body = maps_router.check_service_area(payload, _request_with_tenant())
    assert body["inside_service_area"] is True


def test_service_area_outside_returns_false(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(
        maps_router,
        "get_google_maps_client",
        lambda: FakeGoogleMapsClient(
            geocode_result=[
                {
                    "geometry": {"location": {"lat": 40.71, "lng": -74.00}},
                    "formatted_address": "Outside Address",
                }
            ]
        ),
    )

    polygon = [
        [-87.70, 41.80],
        [-87.50, 41.80],
        [-87.50, 41.95],
        [-87.70, 41.95],
        [-87.70, 41.80],
    ]
    payload = maps_router.ServiceAreaCheckIn(address="Outside", service_polygon=polygon)
    body = maps_router.check_service_area(payload, _request_with_tenant())
    assert body["inside_service_area"] is False


def test_no_api_key_returns_503(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    with pytest.raises(HTTPException) as exc:
        maps_router.get_google_maps_client()
    assert exc.value.status_code == 503
    assert exc.value.detail == "Google Maps not configured"


def test_requires_auth():
    geocode_route = next(r for r in maps_router.router.routes if getattr(r, "path", "") == "/api/maps/geocode")
    dep_calls = [d.call for d in geocode_route.dependant.dependencies]
    assert get_current_user in dep_calls

    from types import SimpleNamespace
    fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(fake_request, token="not-a-valid-jwt"))
    assert exc.value.status_code == 401
