#!/usr/bin/env python3
"""
One-time demo seed for the public gdxdispatch.com instance.

Runs INSIDE the demo app container (has the app env + package on PYTHONPATH):

    docker exec -i gdx-demo-app-1 python /app/gdx_dispatch/docker/demo/seed_demo.py

What it does:
  1. Clears `must_change_password` on the demo admin so the shared login lands
     straight on the dashboard (no forced-change prompt). The password itself is
     set by the app's own bootstrap from GDX_ADMIN_PASSWORD — we never re-hash
     it here, so it always matches what /auth/login verifies.
  2. Seeds a believable garage-door business: customers, jobs across the real
     lifecycle stages, and invoices across the real AR statuses. Each invoice
     bills a seeded job, is built from line items, is taxed by the
     tax-follows-the-job rule (tax.service.resolve_rate_for_job), and is paid
     by a Payment row when it is paid. Uses only model-verified fields + enum
     values (tenant_models.py).
  3. Ensures the dispatch board's holding areas by name, every run, and parks
     a few open jobs in them, so the board's lanes and "Move to…" show.

Idempotent-ish: if the tenant already has live customers, business seeding is
skipped. The nightly reset (reset-demo.sh) wipes the DB first and re-runs this,
so a full fresh, today-relative dataset is regenerated each day. Re-running
against a populated DB only re-asserts the role logins + admin flag.

Fields/enums verified against gdx_dispatch/models/tenant_models.py:
  Job.lifecycle_stage ∈ lead|service_call|estimate|scheduled|in_progress|completed|cancelled
  Job.dispatch_status ∈ unassigned|assigned|en_route|on_site|done
  Job.billing_status  ∈ unbilled|invoiced|partial_paid|paid|overdue|void
  Invoice.status      ∈ draft|sent|paid|overdue|void
  Invoice.billing_type∈ standard|deposit|progress|final
  Invoice.public_token is NOT NULL/unique → must be set explicitly.
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.pricing_provenance import build_invoice_line
from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.models.tenant_models import (
    Customer,
    HoldingArea,
    Invoice,
    Job,
    Payment,
    Technician,
    User,
)
from gdx_dispatch.modules.equipment.models import CustomerEquipment
from gdx_dispatch.modules.numbering import allocate_job_number
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.service import resolve_rate_for_job

NOW = datetime.now(timezone.utc)
ADMIN_EMAIL = os.getenv("GDX_ADMIN_EMAIL", "demo@gdxdispatch.com")  # pii-ok: public demo login
# Shared demo password — same one the landing buttons use for every role.
DEMO_PASSWORD = os.getenv("GDX_ADMIN_PASSWORD", "GdxDemo2026!")

# Role-based demo logins (besides the bootstrap owner = ADMIN_EMAIL). Canonical
# role strings per core/roles.py; werkzeug hash is accepted by /auth/login.
#   (email, role, full_name, schedulable)
ROLE_USERS = [
    ("dispatch@gdxdispatch.com", "dispatcher", "Dana Dispatch", False),  # pii-ok: public demo login
    ("tech@gdxdispatch.com", "technician", "Tyler Tech", True),  # pii-ok: public demo login
]

# (name, email, phone, address, customer_type, pricing_class)
CUSTOMERS = [
    ("Acme Residential", "owner@acme-res.example", "(602) 555-0101", "1420 N Palm Dr, Phoenix, AZ 85014", "Retail", "retail"),
    ("Smith Family", "j.smith@example.com", "(602) 555-0102", "88 Willow Creek Ln, Mesa, AZ 85201", "Retail", "retail"),
    ("Johnson Estates HOA", "manager@johnsonestates.example", "(480) 555-0103", "200 Estate Pkwy, Scottsdale, AZ 85251", "Commercial", "contractor"),
    ("Desert Sun Property Mgmt", "maint@desertsunpm.example", "(480) 555-0104", "55 Sunray Blvd, Tempe, AZ 85281", "Commercial", "contractor"),
    ("Maria Gonzalez", "maria.g@example.com", "(623) 555-0105", "317 Cactus Bloom Way, Glendale, AZ 85301", "Retail", "retail"),
    ("Pinnacle Builders LLC", "ops@pinnaclebuilders.example", "(602) 555-0106", "9 Industrial Rd, Phoenix, AZ 85040", "Commercial", "wholesale"),
    ("Tom & Linda Becker", "becker.home@example.com", "(480) 555-0107", "742 Quail Run, Chandler, AZ 85224", "Retail", "retail"),
    ("Valley Self Storage", "facilities@valleystorage.example", "(623) 555-0108", "1500 Commerce Ave, Peoria, AZ 85345", "Commercial", "contractor"),
    ("Robert Nguyen", "r.nguyen@example.com", "(602) 555-0109", "63 Saguaro Hills Ct, Phoenix, AZ 85022", "Retail", "retail"),
    ("Copperline Apartments", "pm@copperline.example", "(480) 555-0110", "410 Copper St, Gilbert, AZ 85234", "Commercial", "contractor"),
]

# (customer_index, title, job_type, lifecycle, dispatch, billing, days_offset)
#   days_offset < 0 → past (completed), > 0 → upcoming (scheduled)
JOBS = [
    (0, "Broken spring replacement", "Repair", "completed", "done", "paid", -21),
    (0, "Annual tune-up", "Maintenance", "scheduled", "assigned", "unbilled", 4),
    (1, "Opener won't close — sensor fault", "Repair", "completed", "done", "invoiced", -9),
    (2, "Replace 6 community gate panels", "Install", "in_progress", "on_site", "unbilled", 0),
    (3, "Quarterly PM — 12 doors", "Maintenance", "scheduled", "assigned", "unbilled", 7),
    (4, "Off-track door realignment", "Repair", "completed", "done", "paid", -3),
    (5, "New construction — 4 door install", "Install", "estimate", "unassigned", "unbilled", 14),
    (6, "Remote reprogramming", "Service", "completed", "done", "overdue", -30),
    (7, "Roll-up door cable repair", "Repair", "scheduled", "en_route", "unbilled", 1),
    (8, "Weather seal replacement", "Service", "completed", "done", "invoiced", -6),
    (9, "Spring + roller package", "Repair", "scheduled", "assigned", "unbilled", 2),
    (1, "Panel dent — warranty follow-up", "Service", "service_call", "unassigned", "unbilled", 9),
    (5, "Loading dock leveler service", "Maintenance", "completed", "done", "paid", -45),
    (3, "Emergency — door stuck open", "Repair", "completed", "done", "paid", -1),
]

# The demo tenant's sales-tax setup, seeded with the business data so the
# rate the invoices are billed at is the rate Settings shows.
DEMO_TAX_RATE = "0.0825"
# Wholesale reseller with a resale certificate: never taxed, on any job.
EXEMPT_CUSTOMER_INDEXES = (5,)

# Every invoice bills a seeded job (index into JOBS) and is built from lines;
# subtotal, tax and total are computed from those lines in seed(), never
# typed in. Tax follows the job (CLAUDE.md, Doug 2026-10-12/13): parts on a
# repair/service job are taxed at the tenant default, an install is not, an
# exempt customer never is, and labor is untaxed unless TaxConfig.tax_labor.
#   (job_index, status, days_offset_invoice_date,
#    [(description, category, quantity, unit_price), ...])
INVOICES = [
    (0, "paid", -20, [
        ("Torsion spring pair, .250 x 2\" x 32\"", "Parts", "1", "145.00"),
        ("Spring replacement labor", "Labor", "1.5", "95.00"),
    ]),
    (2, "sent", -8, [
        ("Safety sensor pair", "Parts", "1", "64.00"),
        ("Opener logic board", "Parts", "1", "189.00"),
        ("Diagnose and repair opener", "Labor", "2", "95.00"),
    ]),
    (5, "paid", -3, [
        ("Nylon rollers, 13-ball (set of 10)", "Parts", "1", "58.00"),
        ("Lift cable pair", "Parts", "1", "32.00"),
        ("Realign track and rehang door", "Labor", "1.5", "95.00"),
    ]),
    (9, "sent", -6, [
        ("Bottom weather seal, 18 ft", "Parts", "1", "46.00"),
        ("Vinyl stop molding, 9 ft (pair)", "Parts", "1", "38.00"),
        ("Weather seal replacement labor", "Labor", "1", "95.00"),
    ]),
    (12, "paid", -44, [  # exempt customer: untaxed
        ("Dock leveler hydraulic cylinder", "Parts", "1", "1240.00"),
        ("Hydraulic fluid and seal kit", "Parts", "1", "185.00"),
        ("Leveler service labor", "Labor", "6", "95.00"),
    ]),
    (13, "paid", -1, [
        ("Lift cable pair", "Parts", "1", "32.00"),
        ("Cable drum pair", "Parts", "1", "88.00"),
        ("Emergency after-hours labor", "Labor", "2", "145.00"),
    ]),
    (7, "overdue", -30, [
        ("3-button remote, Security+ 2.0", "Parts", "2", "39.00"),
        ("Remote programming labor", "Labor", "0.5", "95.00"),
    ]),
    (3, "draft", 0, [  # installation: untaxed
        ("Replacement gate panel, 24 ga steel", "Parts", "6", "385.00"),
        ("Panel installation labor", "Labor", "8", "95.00"),
    ]),
]

# The demo TECH's route for TODAY — populates the /mobile "Today's Route" and the
# dashboard "Assigned Today" count. assigned_to is set to the Technician.id (NOT
# the user id); scheduled_at is today at the given hour. Nightly reset re-seeds so
# these are always "today". (customer_index, title, hour_utc, lifecycle)
TECH_JOBS = [
    (1, "Broken spring replacement", 8, "in_progress"),
    (4, "Opener tune-up & remote sync", 10, "scheduled"),
    (6, "Off-track door realignment", 12, "scheduled"),
    (0, "Roll-up door cable repair", 14, "scheduled"),
    (8, "Weather seal + roller package", 15, "scheduled"),
]

# The dispatch board's holding areas (GDXA-1454). With none, the board draws
# no lanes and hides "Move to…" (DispatchView gates both on every area but
# Ready to Schedule). The names are the ones the app routes to itself:
# routers/holding_areas.DEFAULT_AREAS, plus "Order Doors", where an accepted
# estimate's job lands (routers/estimates). Ensured by name on every run, so
# a re-seed adds only the missing ones.  (name, color)
HOLDING_AREAS = [
    ("Ready to Schedule", "#10b981"),
    ("Needs Parts", "#f59e0b"),
    ("Waiting on Customer", "#8b5cf6"),
    ("Order Doors", "#0ea5e9"),
]

# Undated, open jobs parked in a lane. An undated open job is on every day
# the board opens, so the lanes are never empty.
#   (customer_index, title, job_type, lifecycle, area_name)
PARKED_JOBS = [
    (2, "Commercial opener replacement — motor on backorder", "Repair", "service_call", "Needs Parts"),
    (9, "Rusted bottom bracket pair — parts ordered", "Repair", "service_call", "Needs Parts"),
    (4, "Door style choice — sent three quotes", "Service", "service_call", "Waiting on Customer"),
    (6, "Two 16×7 insulated doors — on order", "Installation", "scheduled", "Order Doors"),
]


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def seed() -> None:
    tenant_id = company_id()
    db = SessionLocal()
    try:
        # 1. Make the shared demo login land on the dashboard directly.
        admin = db.execute(
            select(User).where(User.email == ADMIN_EMAIL)
        ).scalars().first()
        if admin is not None:
            admin.must_change_password = False
            db.commit()
            print(f"[seed] cleared must_change_password for {ADMIN_EMAIL}")
        else:
            print(f"[seed] WARNING: admin {ADMIN_EMAIL} not found (bootstrap may not have run)")

        # 1b. Ensure the role-based demo logins exist (idempotent). Runs every
        #     time, independent of the business-data guard below.
        tech_user = None
        for email, role, full_name, schedulable in ROLE_USERS:
            u = db.execute(
                select(User).where(User.email == email)
            ).scalars().first()
            if u is None:
                u = User(company_id=tenant_id, email=email, username=email,
                         full_name=full_name, name=full_name)
                db.add(u)
                verb = "created"
            else:
                verb = "refreshed"
            u.role = role
            u.active = True
            u.schedulable = schedulable
            u.must_change_password = False
            u.password_hash = generate_password_hash(DEMO_PASSWORD)
            if role == "technician":
                tech_user = u
            print(f"[seed] {verb} {role} login {email}")
        db.commit()

        # 1c. The mobile "Today's Route" matches Job.assigned_to == Technician.id
        #     (a Technician row linked to the user), so ensure that row exists and
        #     capture its id for job assignment below.
        tech_id = None
        if tech_user is not None:
            t = db.execute(
                select(Technician).where(
                    Technician.user_id == str(tech_user.id),
                )
            ).scalars().first()
            if t is None:
                t = Technician(company_id=tenant_id, user_id=str(tech_user.id),
                               name=tech_user.full_name, email=tech_user.email, active=True)
                db.add(t)
                db.flush()
            tech_id = str(t.id)
            db.commit()
            print(f"[seed] technician profile {tech_id} -> {tech_user.email}")

        # 1d. The board's holding areas, by name, every run.
        live_areas = {
            a.name: a.id for a in db.execute(
                select(HoldingArea).where(HoldingArea.deleted_at.is_(None))
            ).scalars()
        }
        for order, (name, color) in enumerate(HOLDING_AREAS):
            if name not in live_areas:
                area = HoldingArea(id=str(uuid4()), company_id=tenant_id, name=name,
                                   color=color, sort_order=order, created_at=NOW)
                db.add(area)
                live_areas[name] = area.id
                print(f"[seed] holding area {name}")
        db.commit()

        # 2. Skip business seeding if data already exists.
        existing = db.execute(
            select(Customer).where(
                Customer.deleted_at.is_(None)
            ).limit(1)
        ).scalars().first()
        if existing is not None:
            print("[seed] customers already present — skipping business seed.")
            return

        cust_ids = []
        for name, email, phone, address, ctype, pclass in CUSTOMERS:
            c = Customer(
                name=name, email=email, phone=phone, address=address,
                customer_type=ctype, pricing_class=pclass,
                company_id=tenant_id, source="demo",
            )
            db.add(c)
            db.flush()
            cust_ids.append(c.id)
        print(f"[seed] {len(cust_ids)} customers")

        jobs = []
        for ci, title, jtype, lifecycle, dispatch, billing, off in JOBS:
            sched = NOW + timedelta(days=off)
            completed = sched if lifecycle == "completed" else None
            jobs.append(Job(
                customer_id=cust_ids[ci], title=title, job_type=jtype,
                job_number=allocate_job_number(tenant_id, customer_name=CUSTOMERS[ci][0]),
                lifecycle_stage=lifecycle, dispatch_status=dispatch,
                billing_status=billing, status=lifecycle,
                scheduled_at=sched, completed_at=completed,
                priority="Normal", is_demo=True, source="demo",
                company_id=tenant_id,
            ))
        db.add_all(jobs)
        db.flush()
        print(f"[seed] {len(jobs)} jobs")

        for ci, title, jtype, lifecycle, area_name in PARKED_JOBS:
            db.add(Job(
                customer_id=cust_ids[ci], title=title, job_type=jtype,
                job_number=allocate_job_number(tenant_id, customer_name=CUSTOMERS[ci][0]),
                lifecycle_stage=lifecycle, dispatch_status="unassigned",
                billing_status="unbilled", status=lifecycle,
                holding_area_id=live_areas[area_name],
                priority="Normal", is_demo=True, source="demo",
                company_id=tenant_id,
            ))
        print(f"[seed] {len(PARKED_JOBS)} jobs parked in holding areas")

        # Today's route for the demo tech (assigned_to = Technician.id).
        if tech_id:
            tech_job_count = 0
            for ci, title, hour, lifecycle in TECH_JOBS:
                sched = NOW.replace(hour=hour, minute=0, second=0, microsecond=0)
                db.add(Job(
                    customer_id=cust_ids[ci], title=title, job_type="Service",
                    job_number=allocate_job_number(tenant_id, customer_name=CUSTOMERS[ci][0]),
                    lifecycle_stage=lifecycle,
                    dispatch_status=("on_site" if lifecycle == "in_progress" else "assigned"),
                    billing_status="unbilled", status=lifecycle,
                    scheduled_at=sched, assigned_to=tech_id,
                    priority="Normal", is_demo=True, source="demo",
                    company_id=tenant_id,
                ))
                tech_job_count += 1
            print(f"[seed] {tech_job_count} jobs on the tech's route for today")

            # An INSTALL job on the tech's route today + the door/opener
            # equipment (CustomerEquipment is the app's real door/equipment
            # model). The mobile "Install & equipment" panel this fed was
            # removed in #735; the rows still show on the customer record.
            install_cust = Customer(
                name="Westgate Custom Homes (New Build)",
                email="builds@westgatehomes.example", phone="(480) 555-0177",
                address="3300 W Cortez St, Phoenix, AZ 85029",
                customer_type="Commercial", pricing_class="contractor",
                company_id=tenant_id, source="demo",
            )
            db.add(install_cust)
            db.flush()
            db.add(Job(
                customer_id=install_cust.id,
                job_number=allocate_job_number(tenant_id, customer_name=install_cust.name),
                title="New garage door installation — 9×7 insulated steel",
                job_type="Installation", lifecycle_stage="scheduled",
                dispatch_status="assigned", billing_status="unbilled",
                status="scheduled",
                scheduled_at=NOW.replace(hour=9, minute=0, second=0, microsecond=0),
                assigned_to=tech_id, priority="High", is_demo=True,
                source="demo", company_id=tenant_id,
            ))
            today = NOW.date()
            db.add(CustomerEquipment(
                customer_id=install_cust.id, equipment_type="garage_door",
                manufacturer="Clopay", model="Gallery GD2LU 9×7",
                serial_number="CLP-2026-004173", installation_date=today,
                warranty_expires_on=today + timedelta(days=365 * 10),
                notes=("9'0\"W x 7'0\"H | 3-layer insulated steel | R-18.4 | "
                       "Desert Tan | short panel w/ Stockton windows | "
                       "Torsion spring 2.0\" x .250 x 32\" (15,000-cycle)"),
            ))
            db.add(CustomerEquipment(
                customer_id=install_cust.id, equipment_type="opener",
                manufacturer="LiftMaster", model="8500W Wall-Mount",
                serial_number="LM-8500W-022931", installation_date=today,
                warranty_expires_on=today + timedelta(days=365 * 5),
                notes=("Wall-mount DC motor | MyQ Wi-Fi | battery backup | "
                       "2x 380UP remotes + 877MAX wireless keypad"),
            ))
            # Give an existing service stop (Smith Family spring job) a door on
            # file too, so the feature also shows on a service call.
            db.add(CustomerEquipment(
                customer_id=cust_ids[1], equipment_type="garage_door",
                manufacturer="Amarr", model="Lincoln 3000 9×7",
                serial_number="AMR-2019-118842",
                installation_date=date(2019, 4, 12),
                warranty_expires_on=date(2029, 4, 12),
                notes=("9x7 steel | white | broken LH torsion spring (this visit) | "
                       "Genie opener (2024)"),
            ))
            print("[seed] install job + door/opener equipment seeded")

        # Tax setup before the invoices: resolve_rate_for_job reads both.
        tax_cfg = db.execute(select(TaxConfig).limit(1)).scalars().first()
        if tax_cfg is None:
            tax_cfg = TaxConfig(
                name="Default", default_rate=Decimal(DEMO_TAX_RATE),
                tax_labor=False, configured_at=NOW,
            )
            db.add(tax_cfg)
        for ci in EXEMPT_CUSTOMER_INDEXES:
            db.add(TaxExemption(
                customer_id=cust_ids[ci], exempt=True,
                reason="Resale certificate on file",
                certificate_id=f"DEMO-RESALE-{ci + 1:04d}",
            ))
        db.flush()

        inv_count = 0
        for ji, status, off, lines in INVOICES:
            job = jobs[ji]
            rate = resolve_rate_for_job(db, job.customer_id, job.job_type)
            inv_date = (NOW + timedelta(days=off)).date()
            paid = status == "paid"
            invoice = Invoice(
                customer_id=job.customer_id, job_id=job.id,
                invoice_number=f"INV-2026-{1000 + inv_count}",
                public_token=secrets.token_urlsafe(24),
                billing_type="standard", sequence_number=inv_count + 1,
                tax_rate=rate, status=status,
                invoice_date=inv_date, due_date=inv_date + timedelta(days=15),
                sent_at=(NOW + timedelta(days=off) if status != "draft" else None),
                paid_at=(NOW + timedelta(days=off) if paid else None),
                company_id=tenant_id,
            )
            db.add(invoice)
            db.flush()
            subtotal = taxable = Decimal("0")
            for desc, category, qty, unit in lines:
                line_total = _money(Decimal(qty) * Decimal(unit))
                is_taxable = category != "Labor" or bool(tax_cfg.tax_labor)
                # "manual": typed-in demo prices; the labor hours are demo
                # figures with no time entries behind them, not attested.
                db.add(build_invoice_line(
                    pricing_source="manual", invoice_id=invoice.id, description=desc, category=category,
                    quantity=Decimal(qty), unit_price=Decimal(unit),
                    line_total=line_total, taxable=is_taxable,
                    company_id=tenant_id,
                ))
                subtotal += line_total
                if is_taxable:
                    taxable += line_total
            # Same arithmetic as invoices._recalculate_invoice on a
            # rate-aware invoice.
            invoice.subtotal = _money(subtotal)
            invoice.tax_amount = _money(taxable * rate)
            invoice.total = invoice.subtotal + invoice.tax_amount
            invoice.balance_due = invoice.total
            if paid:
                # A paid invoice is paid by a payment row, not by a zeroed
                # balance with nothing behind it.
                db.add(Payment(
                    invoice_id=invoice.id, amount=invoice.total,
                    method="check", reference=f"CHK {4101 + inv_count}",
                    payment_date=inv_date, company_id=tenant_id,
                ))
                invoice.balance_due = Decimal("0.00")
            inv_count += 1
        print(f"[seed] {inv_count} invoices, each built from its lines and billed to its job")

        db.commit()
        print("[seed] done.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
