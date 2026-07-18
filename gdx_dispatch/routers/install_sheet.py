"""Install Sheet — printable spec sheet for technicians on job sites.

Pulls job info, customer details, estimate line items, and CHI door specs
into a printable HTML page (or PDF when WeasyPrint is available).
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID as _UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.door_specs import door_specs_for_job, flatten_door_spec
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Customer, Job, Technician
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["install-sheet"])


def _val(v: Any, default: str = "—") -> str:
    if v is None:
        return default
    return str(v)


def _fdate(v: Any) -> str:
    if not v:
        return "—"
    try:
        return v.strftime("%B %d, %Y at %I:%M %p")
    except Exception:
        logging.getLogger(__name__).exception("_fdate caught exception")
        return str(v)[:16]


def _load_template():
    """Load the install sheet Jinja2 template."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader
    tmpl_dir = Path(__file__).resolve().parent.parent / "templates" / "public"
    env = Environment(loader=FileSystemLoader(str(tmpl_dir)), autoescape=True)
    return env.get_template("install_sheet.html")


@router.get("/api/technicians/daily-loadsheet",
            dependencies=[Depends(require_module("jobs"))])
def daily_loadsheet(
    request: Request,
    date: str = Query(default=""),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Daily load sheet — aggregated parts checklist across all jobs for today."""
    from datetime import date as _date_type
    user_id = str(_user.get("sub") or _user.get("user_id") or "")

    if not date:
        date = _date_type.today().isoformat()

    # Find technician linked to this user
    tech_obj = db.execute(
        select(Technician).where(
            Technician.deleted_at.is_(None),
            (Technician.user_id == user_id) | (Technician.id == user_id),
        ).limit(1)
    ).scalar_one_or_none()
    tech_name = tech_obj.name if tech_obj else "Technician"
    tech_id = str(tech_obj.id) if tech_obj else user_id

    # ORM-routed so customer.address (EncryptedString) decrypts via
    # process_result_value. Pre-S122-9 raw-SQL form rendered ciphertext
    # to the tech's daily route sheet — the load-bearing field workflow.
    from sqlalchemy import cast as _cast, func as _func, or_  # noqa: PLC0415
    from sqlalchemy.types import String as _SaString  # noqa: PLC0415

    rows = (
        db.query(Job, Customer)
        .outerjoin(Customer, Customer.id == Job.customer_id)
        .filter(
            Job.deleted_at.is_(None),
            or_(
                _cast(Job.assigned_to, _SaString) == tech_id,
                _cast(Job.assigned_to, _SaString) == user_id,
            ),
            _func.date(Job.scheduled_at) == date,
        )
        .order_by(Job.scheduled_at)
        .all()
    )
    jobs = [
        {
            "id": j.id,
            "title": j.title,
            "job_type": j.job_type,
            "priority": j.priority,
            "customer_name": c.name if c else None,
            "address": c.address if c else None,
            "phone": c.phone if c else None,
        }
        for j, c in rows
    ]

    job_list = []
    all_items: dict[str, dict] = {}  # keyed by description

    for job in jobs:
        jid = str(job["id"])
        job_list.append({
            "id": jid, "title": job["title"], "job_type": job["job_type"],
            "customer": job["customer_name"] or "—", "address": job["address"] or "—",
            "phone": job["phone"] or "", "priority": job["priority"] or "Normal",
        })

        # Get estimate lines for this job
        try:
            _jid_uuid = _UUID(jid)
        except (ValueError, AttributeError):
            logging.getLogger(__name__).exception("daily_loadsheet caught exception")
            continue
        est_obj = db.execute(
            select(Estimate).where(
                Estimate.job_id == _jid_uuid,
                Estimate.deleted_at.is_(None),
            ).order_by(Estimate.created_at.desc()).limit(1)
        ).scalar_one_or_none()

        if not est_obj:
            continue

        line_rows = db.execute(
            select(EstimateLine).where(
                EstimateLine.estimate_id == est_obj.id,
            ).order_by(EstimateLine.id)
        ).scalars().all()
        lines = [{"description": ln.description, "quantity": ln.quantity, "unit_price": float(ln.unit_price or 0)} for ln in line_rows]

        for line in lines:
            desc = (line["description"] or "").strip()
            if not desc:
                continue
            qty = int(float(line["quantity"] or 1))

            # Categorize
            dl = desc.lower()
            if any(k in dl for k in ["door", "panel", "section", "chi"]):
                cat = "Doors"
            elif any(k in dl for k in ["opener", "drive", "motor"]):
                cat = "Openers"
            elif any(k in dl for k in ["spring", "torsion", "extension"]):
                cat = "Springs"
            elif any(k in dl for k in ["labor", "service", "install", "diagnostic", "tune"]):
                cat = "Labor"
            else:
                cat = "Parts"

            if desc not in all_items:
                all_items[desc] = {"description": desc, "category": cat, "total_qty": 0, "jobs": []}
            all_items[desc]["total_qty"] += qty
            all_items[desc]["jobs"].append({
                "job_id": jid, "customer": job["customer_name"] or "—", "qty": qty,
            })

    # Sort: Doors first, then Springs, Openers, Parts, Labor
    cat_order = {"Doors": 0, "Springs": 1, "Openers": 2, "Parts": 3, "Labor": 4}
    items = sorted(all_items.values(), key=lambda x: (cat_order.get(x["category"], 9), x["description"]))

    return {
        "date": date,
        "technician_name": tech_name,
        "jobs": job_list,
        "items": items,
        "total_items": len(items),
        "total_jobs": len(job_list),
    }


@router.get("/api/jobs/{job_id}/install-specs",
            dependencies=[Depends(require_module("jobs"))])
def install_specs(
    job_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """JSON endpoint — returns door specs + parts for the install tab."""
    # Get estimate for this job via ORM
    try:
        _job_uuid = _UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("install_specs caught exception")
        _job_uuid = None
    estimate_obj = None
    if _job_uuid is not None:
        estimate_obj = db.execute(
            select(Estimate).where(
                Estimate.job_id == _job_uuid,
                Estimate.deleted_at.is_(None),
            ).order_by(Estimate.created_at.desc()).limit(1)
        ).scalar_one_or_none()

    lines = []
    if estimate_obj:
        line_rows = db.execute(
            select(EstimateLine).where(
                EstimateLine.estimate_id == estimate_obj.id,
            ).order_by(EstimateLine.id)
        ).scalars().all()
        for lr in line_rows:
            qty = float(lr.quantity or 1)
            price = float(lr.unit_price or 0)
            lines.append({"description": lr.description, "quantity": int(qty), "unit_price": price})

    # Prefer the ACTUAL captured door(s) carried on the estimate line_metadata
    # (source=chi_hubx) over a catalog lookup — the catalog match below returns a
    # generic catalogued door by fuzzy description, not the specific door that
    # was quoted (its real color, spring, track, windows, price). Fall back to
    # the catalog match only for non-CHI doors, so nothing regresses.
    captured_doors = door_specs_for_job(db, job_id)
    door_specs = flatten_door_spec(captured_doors[0]) if captured_doors else None

    # Find door specs from line items — CHI feed + tenant-custom doors.
    for line in lines:
        if door_specs is not None:
            break
        desc = (line["description"] or "").strip()
        if not desc:
            continue
        try:
            spec = db.execute(text("""
                SELECT model_number, brand, width, height, color, insulation_type, r_value,
                       panel_style, section_construction, window_option, window_type,
                       finish_type, high_lift, high_lift_in, sales_talking_point
                FROM (
                    SELECT model_number, brand, width, height, color, insulation_type, r_value,
                           panel_style, section_construction, window_option, window_type,
                           finish_type, high_lift, high_lift_in, sales_talking_point,
                           description, sku
                    FROM chi_door_catalog
                    UNION ALL
                    SELECT ds.model_number, ds.manufacturer AS brand,
                           ds.width, ds.height, ds.color, ds.insulation_type, ds.r_value,
                           ds.panel_style, ds.section_construction, ds.window_option,
                           ds.window_type, ds.finish_type, ds.high_lift, ds.high_lift_in,
                           ds.sales_talking_point,
                           COALESCE(cci.description, cci.name) AS description,
                           cci.sku AS sku
                    FROM custom_catalog_items cci
                    LEFT JOIN door_specs ds ON ds.catalog_item_id = cci.id
                    WHERE cci.product_class = 'door' AND cci.active = true
                          AND cci.deleted_at IS NULL
                ) AS unioned
                WHERE description ILIKE :q OR model_number ILIKE :q OR sku ILIKE :q
                LIMIT 1
            """), {"q": f"%{desc[:50]}%"}).mappings().first()
            if spec:
                door_specs = {k: (_val(v, "") if not isinstance(v, (int, float)) else v) for k, v in dict(spec).items()}
                break
        except Exception:
            logging.getLogger(__name__).exception("install_specs caught exception")
            pass

    return {
        "door_specs": door_specs,
        "lines": lines,
        "notes": estimate_obj.notes if estimate_obj else None,
        "estimate_total": sum(l["quantity"] * l["unit_price"] for l in lines),
    }


@router.get("/api/jobs/{job_id}/install-sheet",
            dependencies=[Depends(require_module("jobs"))])
def install_sheet(
    job_id: str,
    request: Request,
    fmt: str = Query(default="html", alias="format"),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", ""))

    # Get job via ORM
    try:
        _job_uuid = _UUID(job_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Job not found") from None

    from gdx_dispatch.models.tenant_models import Customer
    result = db.execute(
        select(Job, Customer).outerjoin(Customer, Job.customer_id == Customer.id).where(
            Job.id == _job_uuid,
            Job.deleted_at.is_(None),
        )
    ).first()
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    job_obj, customer_obj = result
    job = {
        "id": str(job_obj.id),
        "title": job_obj.title,
        "description": job_obj.description,
        "status": job_obj.status,
        "job_type": job_obj.job_type,
        "priority": job_obj.priority,
        "scheduled_at": job_obj.scheduled_at,
        "assigned_to": job_obj.assigned_to,
        "customer_name": customer_obj.name if customer_obj else None,
        "phone": customer_obj.phone if customer_obj else None,
        "address": customer_obj.address if customer_obj else None,
        "customer_email": customer_obj.email if customer_obj else None,
    }

    # Get estimate + lines via ORM
    estimate_obj = db.execute(
        select(Estimate).where(
            Estimate.job_id == _job_uuid,
            Estimate.deleted_at.is_(None),
        ).order_by(Estimate.created_at.desc()).limit(1)
    ).scalar_one_or_none()

    lines = []
    estimate_total = 0
    if estimate_obj:
        line_rows = db.execute(
            select(EstimateLine).where(
                EstimateLine.estimate_id == estimate_obj.id,
            ).order_by(EstimateLine.id)
        ).scalars().all()
        for lr in line_rows:
            qty = float(lr.quantity or 1)
            price = float(lr.unit_price or 0)
            total = float(lr.line_total or qty * price)
            lines.append({"description": lr.description, "quantity": int(qty), "unit_price": price, "total": total})
            estimate_total += total

    # The ACTUAL captured door(s) quoted on this job, from the estimate
    # line_metadata — the real spec (color/spring/track/windows), not a
    # fuzzy-matched catalogued door. Rendered as a dedicated block on the sheet.
    captured_doors = door_specs_for_job(db, job_id)

    # Try to find door specs (CHI + custom) from line item descriptions — only
    # when there's no captured door to show (non-CHI installs).
    door_specs = None
    for line in lines:
        if captured_doors:
            break
        desc = (line["description"] or "").strip()
        if not desc:
            continue
        spec = db.execute(text("""
            SELECT model_number, brand, width, height, color, insulation_type, r_value,
                   panel_style, section_construction, window_option, window_type,
                   finish_type, high_lift, high_lift_in, sales_talking_point, description
            FROM (
                SELECT model_number, brand, width, height, color, insulation_type, r_value,
                       panel_style, section_construction, window_option, window_type,
                       finish_type, high_lift, high_lift_in, sales_talking_point,
                       description, sku
                FROM chi_door_catalog
                UNION ALL
                SELECT ds.model_number, ds.manufacturer AS brand,
                       ds.width, ds.height, ds.color, ds.insulation_type, ds.r_value,
                       ds.panel_style, ds.section_construction, ds.window_option,
                       ds.window_type, ds.finish_type, ds.high_lift, ds.high_lift_in,
                       ds.sales_talking_point,
                       COALESCE(cci.description, cci.name) AS description,
                       cci.sku AS sku
                FROM custom_catalog_items cci
                LEFT JOIN door_specs ds ON ds.catalog_item_id = cci.id
                WHERE cci.product_class = 'door' AND cci.active = true
                      AND cci.deleted_at IS NULL
            ) AS unioned
            WHERE description ILIKE :q OR model_number ILIKE :q OR sku ILIKE :q
            LIMIT 1
        """), {"q": f"%{desc[:50]}%"}).mappings().first()
        if spec:
            door_specs = dict(spec)
            break

    # Get technician name via ORM
    tech_name = "—"
    if job.get("assigned_to"):
        _atid = str(job["assigned_to"])
        tech_obj = db.execute(
            select(Technician).where(
                (Technician.id == _atid) | (Technician.user_id == _atid),
                Technician.deleted_at.is_(None),
            ).limit(1)
        ).scalar_one_or_none()
        if tech_obj:
            tech_name = tech_obj.name or "—"

    # Categorize parts for template
    parts = []
    for line in lines:
        desc = line["description"] or ""
        dl = desc.lower()
        if any(k in dl for k in ["door", "panel", "section"]):
            cat = "Doors"
        elif any(k in dl for k in ["opener", "drive", "motor"]):
            cat = "Openers"
        elif any(k in dl for k in ["spring", "torsion", "extension"]):
            cat = "Springs"
        elif any(k in dl for k in ["labor", "service", "install", "diagnostic"]):
            cat = "Labor"
        else:
            cat = "Parts"
        parts.append({"description": desc, "category": cat, "quantity": line["quantity"], "unit_price": line["unit_price"], "total": line["total"]})

    # Build door specs display value for high lift
    if door_specs:
        door_specs["high_lift_display"] = f'{door_specs["high_lift_in"]}"' if door_specs.get("high_lift_in") else _val(door_specs.get("high_lift"), "No")

    # "Total-only" display — the install sheet is a customer-signed document, so
    # respect the same effective hide-prices setting the estimate PDF uses
    # (per-estimate override else tenant default). Best-effort; show prices on
    # any read error so the tech sheet is never blank.
    hide_line_prices = False
    if estimate_obj is not None:
        try:
            from gdx_dispatch.modules.estimates_features import (
                effective_hide_line_prices,
                get_features,
            )
            _hide_default = get_features(tenant_id).hide_line_prices if tenant_id else False
            hide_line_prices = effective_hide_line_prices(
                getattr(estimate_obj, "hide_line_prices", None), _hide_default
            )
        except Exception:
            logging.getLogger(__name__).exception("install_sheet hide_line_prices resolve failed")
            hide_line_prices = False

    job_num = f"JOB-{str(job['id'])[:8].upper()}"
    auto_print = False

    if fmt == "pdf":
        try:
            from weasyprint import HTML as WPHTML
            tmpl = _load_template()
            html = tmpl.render(
                company_name="DispatchApp", job_number=job_num,
                customer_name=_val(job.get("customer_name")), address=_val(job.get("address")),
                phone=_val(job.get("phone")), scheduled_at=_fdate(job.get("scheduled_at")),
                technician=tech_name, job_type=_val(job.get("job_type"), "Service"),
                priority=_val(job.get("priority"), "Normal"), door_specs=door_specs,
                captured_doors=captured_doors,
                parts=parts, estimate_total=estimate_total,
                hide_line_prices=hide_line_prices,
                notes=_val(estimate_obj.notes if estimate_obj else job.get("description"), "No special instructions."),
                auto_print=False,
            )
            pdf = WPHTML(string=html).write_pdf()
            return Response(content=pdf, media_type="application/pdf",
                           headers={"Content-Disposition": f"attachment; filename=install_sheet_{job_num}.pdf"})
        except ImportError:
            logging.getLogger(__name__).exception("install_sheet caught exception")
            auto_print = True

    tmpl = _load_template()
    html = tmpl.render(
        company_name="DispatchApp", job_number=job_num,
        customer_name=_val(job.get("customer_name")), address=_val(job.get("address")),
        phone=_val(job.get("phone")), scheduled_at=_fdate(job.get("scheduled_at")),
        technician=tech_name, job_type=_val(job.get("job_type"), "Service"),
        priority=_val(job.get("priority"), "Normal"), door_specs=door_specs,
        captured_doors=captured_doors,
        parts=parts, estimate_total=estimate_total,
        hide_line_prices=hide_line_prices,
        notes=_val(estimate_obj.notes if estimate_obj else job.get("description"), "No special instructions."),
        auto_print=auto_print,
    )

    return HTMLResponse(content=html)
