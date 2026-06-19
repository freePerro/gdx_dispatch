from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.contractors.models import Contractor, ContractorAssignment
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(
    prefix="/api",
    tags=["contractors"],
    dependencies=[Depends(require_module("contractors"))],
)


class ContractorIn(BaseModel):
    name: str
    company_name: str | None = None
    phone: str | None = None
    email: str | None = None
    specialty: list[str] = []
    license_number: str | None = None
    insurance_expiry: date | None = None
    hourly_rate: float | None = None
    notes: str | None = None


class ContractorPatch(BaseModel):
    name: str | None = None
    company_name: str | None = None
    phone: str | None = None
    email: str | None = None
    specialty: list[str] | None = None
    license_number: str | None = None
    insurance_expiry: date | None = None
    hourly_rate: float | None = None
    is_active: bool | None = None
    notes: str | None = None


class AssignIn(BaseModel):
    job_id: UUID | None = None
    scheduled_date: date


class CompleteIn(BaseModel):
    hours_worked: float
    notes: str | None = None


# --- List ---

@router.get("/contractors", response_model=None)
def list_contractors(
    skip: int = 0,
    limit: int = 50,
    specialty: str | None = None,
    db: Session = Depends(get_db),
) -> list[Contractor]:
    q = select(Contractor).where(
        Contractor.deleted_at.is_(None),
        Contractor.is_active.is_(True),
    )
    if specialty:
        # JSON list contains check — works with PostgreSQL json_contains and SQLite JSON
        q = q.where(Contractor.specialty.contains([specialty]))
    q = q.order_by(Contractor.created_at.desc()).offset(skip).limit(limit)
    return list(db.execute(q).scalars().all())


# --- Expiring insurance (MUST be before /{contractor_id} to avoid routing conflict) ---

@router.get("/contractors/expiring-insurance", response_model=None)
def expiring_insurance(db: Session = Depends(get_db)) -> list[Contractor]:
    today = date.today()
    cutoff = today + timedelta(days=60)
    q = select(Contractor).where(
        Contractor.deleted_at.is_(None),
        Contractor.insurance_expiry.isnot(None),
        Contractor.insurance_expiry >= today,
        Contractor.insurance_expiry <= cutoff,
    )
    return list(db.execute(q.order_by(Contractor.insurance_expiry.asc())).scalars().all())


# --- Available contractors for a given date (MUST be before /{contractor_id}) ---

@router.get("/contractors/available", response_model=None)
def available_contractors(
    scheduled_date: date = Query(..., description="Date to check availability (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
) -> list[Contractor]:
    """Return contractors that have no assignment on the given date."""
    busy_subq = select(ContractorAssignment.contractor_id).where(
        ContractorAssignment.scheduled_date == scheduled_date
    ).scalar_subquery()
    q = select(Contractor).where(
        Contractor.deleted_at.is_(None),
        Contractor.is_active.is_(True),
        Contractor.id.not_in(busy_subq),
    ).order_by(Contractor.name.asc())
    return list(db.execute(q).scalars().all())


# --- Create ---

@router.post("/contractors", response_model=None)
def create_contractor(
    payload: ContractorIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Contractor:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    contractor = Contractor(**payload.model_dump())
    db.add(contractor)
    db.commit()
    db.refresh(contractor)
    return contractor


# --- Get by ID with assignments ---

@router.get("/contractors/{contractor_id}", response_model=None)
def get_contractor(contractor_id: UUID, db: Session = Depends(get_db)) -> dict:
    contractor = db.execute(
        select(Contractor).where(
            Contractor.id == contractor_id,
            Contractor.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    assignments = list(
        db.execute(
            select(ContractorAssignment)
            .where(ContractorAssignment.contractor_id == contractor_id)
            .order_by(ContractorAssignment.scheduled_date.desc())
        ).scalars().all()
    )
    return {"contractor": contractor, "assignments": assignments}


# --- Update ---

@router.put("/contractors/{contractor_id}", response_model=None)
def update_contractor(
    contractor_id: UUID,
    payload: ContractorPatch,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Contractor:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    contractor = db.execute(
        select(Contractor).where(
            Contractor.id == contractor_id,
            Contractor.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(contractor, k, v)
    db.commit()
    db.refresh(contractor)
    return contractor


# --- Soft delete ---

@router.delete("/contractors/{contractor_id}", response_model=None)
def delete_contractor(
    contractor_id: UUID,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    contractor = db.execute(
        select(Contractor).where(
            Contractor.id == contractor_id,
            Contractor.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    contractor.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return {"deleted": True}


# --- Assign to job ---

@router.post("/contractors/{contractor_id}/assign", response_model=None)
def assign_contractor(
    contractor_id: UUID,
    payload: AssignIn,
    db: Session = Depends(get_db),
) -> ContractorAssignment:
    contractor = db.execute(
        select(Contractor).where(
            Contractor.id == contractor_id,
            Contractor.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    assignment = ContractorAssignment(
        contractor_id=contractor_id,
        job_id=payload.job_id,
        scheduled_date=payload.scheduled_date,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment


# --- Complete assignment ---

@router.post("/contractors/{contractor_id}/assignments/{assignment_id}/complete", response_model=None)
def complete_assignment(
    contractor_id: UUID,
    assignment_id: UUID,
    payload: CompleteIn,
    db: Session = Depends(get_db),
) -> ContractorAssignment:
    assignment = db.execute(
        select(ContractorAssignment).where(
            ContractorAssignment.id == assignment_id,
            ContractorAssignment.contractor_id == contractor_id,
        )
    ).scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    assignment.hours_worked = payload.hours_worked
    if payload.notes is not None:
        assignment.notes = payload.notes
    assignment.status = "completed"
    # Calculate total cost if hourly rate is set
    contractor = db.execute(
        select(Contractor).where(Contractor.id == contractor_id)
    ).scalar_one_or_none()
    if contractor and contractor.hourly_rate is not None:
        assignment.total_cost = float(contractor.hourly_rate) * payload.hours_worked
    db.commit()
    db.refresh(assignment)
    return assignment


# --- PATCH (partial update) ---

@router.patch("/contractors/{contractor_id}", response_model=None)
def patch_contractor(
    contractor_id: UUID,
    payload: ContractorPatch,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Contractor:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    contractor = db.execute(
        select(Contractor).where(
            Contractor.id == contractor_id,
            Contractor.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(contractor, k, v)
    db.commit()
    db.refresh(contractor)
    return contractor


# --- Jobs assigned to a contractor ---

@router.get("/contractors/{contractor_id}/jobs", response_model=None)
def list_contractor_jobs(
    contractor_id: UUID,
    db: Session = Depends(get_db),
) -> list[ContractorAssignment]:
    """Return all assignments for a contractor that have a job_id set."""
    q = (
        select(ContractorAssignment)
        .where(
            ContractorAssignment.contractor_id == contractor_id,
            ContractorAssignment.job_id.isnot(None),
        )
        .order_by(ContractorAssignment.scheduled_date.desc())
    )
    return list(db.execute(q).scalars().all())


# --- Assign contractor to job via path params ---

@router.post("/contractors/{contractor_id}/assign/{job_id}", response_model=None)
def assign_contractor_to_job(
    contractor_id: UUID,
    job_id: UUID,
    scheduled_date: date = Query(..., description="Date of the assignment (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
) -> ContractorAssignment:
    contractor = db.execute(
        select(Contractor).where(
            Contractor.id == contractor_id,
            Contractor.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    assignment = ContractorAssignment(
        contractor_id=contractor_id,
        job_id=job_id,
        scheduled_date=scheduled_date,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment
