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
     lifecycle stages, and invoices across the real AR statuses. Uses only
     model-verified fields + enum values (tenant_models.py).

Idempotent-ish: if the tenant already has live customers, business seeding is
skipped (the nightly reset restores a golden snapshot anyway). Re-running only
re-asserts the admin flag.

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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, User

NOW = datetime.now(timezone.utc)
ADMIN_EMAIL = os.getenv("GDX_ADMIN_EMAIL", "demo@gdxdispatch.com")

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
    (6, "Remote reprogramming", "Service", "completed", "done", "paid", -30),
    (7, "Roll-up door cable repair", "Repair", "scheduled", "en_route", "unbilled", 1),
    (8, "Weather seal replacement", "Service", "completed", "done", "invoiced", -6),
    (9, "Spring + roller package", "Repair", "scheduled", "assigned", "unbilled", 2),
    (1, "Panel dent — warranty follow-up", "Service", "service_call", "unassigned", "unbilled", 9),
    (5, "Loading dock leveler service", "Maintenance", "completed", "done", "paid", -45),
    (3, "Emergency — door stuck open", "Repair", "completed", "done", "paid", -1),
]

# (customer_index, status, subtotal, tax, days_offset_invoice_date)
INVOICES = [
    (0, "paid", "285.00", "23.51", -20),
    (1, "sent", "612.40", "50.52", -8),
    (4, "paid", "189.00", "15.59", -3),
    (8, "sent", "342.75", "28.28", -6),
    (5, "paid", "4180.00", "344.85", -44),
    (3, "paid", "525.00", "43.31", -1),
    (6, "overdue", "95.00", "7.84", -52),
    (9, "draft", "740.00", "61.05", 0),
]


def _money(s: str) -> Decimal:
    return Decimal(s)


def seed() -> None:
    tenant_id = company_id()
    db = SessionLocal()
    try:
        # 1. Make the shared demo login land on the dashboard directly.
        admin = db.execute(
            select(User).where(User.email == ADMIN_EMAIL, User.company_id == tenant_id)
        ).scalars().first()
        if admin is not None:
            admin.must_change_password = False
            db.commit()
            print(f"[seed] cleared must_change_password for {ADMIN_EMAIL}")
        else:
            print(f"[seed] WARNING: admin {ADMIN_EMAIL} not found (bootstrap may not have run)")

        # 2. Skip business seeding if data already exists.
        existing = db.execute(
            select(Customer).where(
                Customer.company_id == tenant_id, Customer.deleted_at.is_(None)
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

        job_count = 0
        for ci, title, jtype, lifecycle, dispatch, billing, off in JOBS:
            sched = NOW + timedelta(days=off)
            completed = sched if lifecycle == "completed" else None
            db.add(Job(
                customer_id=cust_ids[ci], title=title, job_type=jtype,
                lifecycle_stage=lifecycle, dispatch_status=dispatch,
                billing_status=billing, status=lifecycle,
                scheduled_at=sched, completed_at=completed,
                priority="Normal", is_demo=True, source="demo",
                company_id=tenant_id,
            ))
            job_count += 1
        print(f"[seed] {job_count} jobs")

        inv_count = 0
        for ci, status, subtotal, tax, off in INVOICES:
            sub, tx = _money(subtotal), _money(tax)
            total = sub + tx
            inv_date = (NOW + timedelta(days=off)).date()
            due = inv_date + timedelta(days=15)
            paid = status == "paid"
            db.add(Invoice(
                customer_id=cust_ids[ci], job_id=None,
                invoice_number=f"INV-2026-{1000 + inv_count}",
                public_token=secrets.token_urlsafe(24),
                billing_type="standard", sequence_number=inv_count + 1,
                subtotal=sub, tax_amount=tx, total=total, total_amount=total,
                balance_due=(Decimal("0.00") if paid else total),
                amount_paid=(total if paid else Decimal("0.00")),
                status=status, invoice_date=inv_date, due_date=due,
                sent_at=(NOW + timedelta(days=off) if status != "draft" else None),
                paid_at=(NOW + timedelta(days=off) if paid else None),
                company_id=tenant_id,
            ))
            inv_count += 1
        print(f"[seed] {inv_count} invoices")

        db.commit()
        print("[seed] done.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
