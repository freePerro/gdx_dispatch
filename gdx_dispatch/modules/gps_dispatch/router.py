from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.modules.gps_dispatch.service import assign_route, update_technician_location

# GET /dispatch/locations left this module 2026-09-06 (#569): routers/tech_locations.py
# registers it first, so FastAPI never dispatched here. The two routes below are
# unique to this module.
router = APIRouter(prefix="/api", tags=["gps_dispatch"], dependencies=[Depends(require_module("gps_dispatch")), Depends(get_current_user)])


@router.post("/dispatch/location", response_model=None)
def post_location(
    payload: dict,
    db: Session = Depends(get_db),
) -> Any:
    location = update_technician_location(
        tech_id=payload["technician_id"],
        lat=float(payload["lat"]),
        lng=float(payload["lng"]),
        accuracy=payload.get("accuracy_meters"),
        db=db,
    )
    return {
        "id": str(location.id),
        "technician_id": location.tech_id,
        "lat": float(location.lat),
        "lng": float(location.lng),
        "recorded_at": location.recorded_at.isoformat(),
        "accuracy_meters": float(location.accuracy_meters) if location.accuracy_meters is not None else None,
    }


@router.post("/dispatch/routes", response_model=None)
def post_route(
    payload: dict,
    db: Session = Depends(get_db),
) -> Any:
    route = assign_route(
        technician_id=payload["technician_id"],
        job_id=UUID(payload["job_id"]),
        distance_km=payload.get("distance_km"),
        db=db,
    )
    return {
        "id": str(route.id),
        "technician_id": route.technician_id,
        "job_id": str(route.job_id),
        "distance_km": float(route.distance_km) if route.distance_km is not None else None,
        "estimated_arrival": route.estimated_arrival.isoformat() if route.estimated_arrival else None,
        "created_at": route.created_at.isoformat(),
    }
