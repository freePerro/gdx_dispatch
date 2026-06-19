from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/maps",
    tags=["maps"],
    dependencies=[Depends(get_current_user), Depends(require_module("google_maps"))],
)


class GeocodeIn(BaseModel):
    address: str = Field(min_length=1, max_length=500)


class ReverseGeocodeIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RouteJobIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=1, max_length=500)


class OptimizeRouteIn(BaseModel):
    tech_start_location: str = Field(min_length=1, max_length=500)
    jobs: list[RouteJobIn] = Field(default_factory=list, max_length=200)


class DriveTimeIn(BaseModel):
    origin: str = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=500)


class ServiceAreaCheckIn(BaseModel):
    address: str = Field(min_length=1, max_length=500)
    service_polygon: list[list[float]] | None = Field(default=None, max_length=1000)


def _google_maps_api_key() -> str:
    key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="Google Maps not configured")
    return key


def get_google_maps_client() -> Any:
    key = _google_maps_api_key()
    try:
        import googlemaps
    except ImportError as exc:
        log.exception("google_maps_import_failed")
        raise HTTPException(status_code=503, detail="Google Maps not configured") from exc
    return googlemaps.Client(key=key)


def _first_geocode_result_or_404(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        raise HTTPException(status_code=404, detail="Address not found")
    return results[0]


@router.post("/geocode")
def geocode_address(payload: GeocodeIn) -> dict[str, Any]:
    gmaps = get_google_maps_client()
    result = _first_geocode_result_or_404(gmaps.geocode(payload.address))
    location = result.get("geometry", {}).get("location", {})
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="geocode_address",
                entity_type="geocode_address",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('geocode_address_audit_failed')
    return {
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "formatted_address": result.get("formatted_address"),
    }


@router.post("/reverse-geocode")
def reverse_geocode(payload: ReverseGeocodeIn) -> dict[str, Any]:
    gmaps = get_google_maps_client()
    result = _first_geocode_result_or_404(gmaps.reverse_geocode((payload.lat, payload.lng)))
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="reverse_geocode",
                entity_type="reverse_geocode",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('reverse_geocode_audit_failed')
    return {"address": result.get("formatted_address")}


@router.post("/optimize-route")
def optimize_route(payload: OptimizeRouteIn) -> dict[str, Any]:
    if not payload.jobs:
        return {"optimized_job_ids": [], "total_duration_seconds": 0, "total_distance_meters": 0}

    gmaps = get_google_maps_client()
    waypoints = [job.address for job in payload.jobs]
    routes = gmaps.directions(
        payload.tech_start_location,
        payload.tech_start_location,
        waypoints=waypoints,
        optimize_waypoints=True,
    )
    if not routes:
        raise HTTPException(status_code=404, detail="Route not found")

    route = routes[0]
    order = route.get("waypoint_order", [])
    optimized_job_ids = [payload.jobs[i].id for i in order]
    legs = route.get("legs", [])
    total_duration = sum(int(leg.get("duration", {}).get("value", 0)) for leg in legs)
    total_distance = sum(int(leg.get("distance", {}).get("value", 0)) for leg in legs)

    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="optimize_route",
                entity_type="optimize_route",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('optimize_route_audit_failed')
    return {
        "optimized_job_ids": optimized_job_ids,
        "total_duration_seconds": total_duration,
        "total_distance_meters": total_distance,
    }


@router.post("/drive-time")
def drive_time(payload: DriveTimeIn) -> dict[str, Any]:
    gmaps = get_google_maps_client()
    matrix = gmaps.distance_matrix([payload.origin], [payload.destination])
    element = ((matrix.get("rows") or [{}])[0].get("elements") or [{}])[0]
    if element.get("status") != "OK":
        raise HTTPException(status_code=404, detail="Drive time not found")

    distance = element.get("distance", {})
    duration = element.get("duration", {})
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="drive_time",
                entity_type="drive_time",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('drive_time_audit_failed')
    return {
        "duration_text": duration.get("text"),
        "duration_seconds": duration.get("value"),
        "distance_text": distance.get("text"),
        "distance_meters": distance.get("value"),
    }


def _resolve_polygon(payload: ServiceAreaCheckIn, request: Request) -> list[list[float]]:
    tenant = getattr(request.state, "tenant", {}) or {}
    polygon = payload.service_polygon or tenant.get("service_polygon") or tenant.get("service_area_polygon")
    if not polygon:
        raise HTTPException(status_code=400, detail="Service polygon not configured")
    if len(polygon) < 3:
        raise HTTPException(status_code=400, detail="Service polygon must have at least 3 points")
    return polygon


def _point_in_polygon(lng: float, lat: float, polygon_coords: list[list[float]]) -> bool:
    # Prefer shapely when available.
    try:
        from shapely.geometry import Point, Polygon

        polygon = Polygon([(float(coord[0]), float(coord[1])) for coord in polygon_coords])
        point = Point(lng, lat)
        return bool(polygon.contains(point) or polygon.touches(point))
    except (ImportError, ModuleNotFoundError):
        log.exception("shapely_import_failed_using_fallback")

    # Fallback: ray-casting containment test with boundary check.
    points = [(float(coord[0]), float(coord[1])) for coord in polygon_coords]
    if len(points) < 3:
        return False

    inside = False
    x, y = lng, lat
    prev_x, prev_y = points[-1]
    for curr_x, curr_y in points:
        if ((curr_y > y) != (prev_y > y)) and (
            x < (prev_x - curr_x) * (y - curr_y) / ((prev_y - curr_y) or 1e-12) + curr_x
        ):
            inside = not inside
        prev_x, prev_y = curr_x, curr_y
    return inside


@router.post("/check-service-area")
def check_service_area(payload: ServiceAreaCheckIn, request: Request) -> dict[str, Any]:
    gmaps = get_google_maps_client()
    geocode_result = _first_geocode_result_or_404(gmaps.geocode(payload.address))
    location = geocode_result.get("geometry", {}).get("location", {})
    lat = float(location.get("lat"))
    lng = float(location.get("lng"))

    polygon_coords = _resolve_polygon(payload, request)
    log.info("service_area_checked", extra={"path": "/api/maps/check-service-area"})
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="check_service_area",
                entity_type="check_service_area",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('check_service_area_audit_failed')
    return {"inside_service_area": _point_in_polygon(lng, lat, polygon_coords), "lat": lat, "lng": lng}
