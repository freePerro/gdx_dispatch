from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.gps_dispatch.models import DispatchRoute, TechnicianLocation


def update_technician_location(
    tech_id: str,
    lat: float,
    lng: float,
    accuracy: float | None,
    db: Session,
    company_id: str = "",
) -> TechnicianLocation:
    """Create a new location record for the given technician."""
    location = TechnicianLocation(
        company_id=company_id,
        tech_id=tech_id,
        lat=lat,
        lng=lng,
        accuracy_meters=accuracy,
        recorded_at=datetime.now(timezone.utc),
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def get_technician_locations(db: Session) -> list[TechnicianLocation]:
    """Get the latest location record per technician using a subquery on max recorded_at."""
    subq = (
        select(
            TechnicianLocation.tech_id,
            func.max(TechnicianLocation.recorded_at).label("max_recorded_at"),
        )
        .group_by(TechnicianLocation.tech_id)
        .subquery()
    )

    stmt = select(TechnicianLocation).join(
        subq,
        (TechnicianLocation.tech_id == subq.c.tech_id)
        & (TechnicianLocation.recorded_at == subq.c.max_recorded_at),
    )

    return list(db.execute(stmt).scalars().all())


def assign_route(
    technician_id: str,
    job_id: UUID,
    distance_km: float | None,
    db: Session,
) -> DispatchRoute:
    """Create a DispatchRoute assigning a technician to a job."""
    route = DispatchRoute(
        technician_id=technician_id,
        job_id=job_id,
        distance_km=distance_km,
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    return route
