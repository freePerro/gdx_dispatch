"""
test_gdpr_enhanced.py — 8 tests for comprehensive GDPR tooling.

Covers:
1. Full tenant export creates a valid ZIP with 5 CSV files
2. Export is rate-limited to 1 per 24 hours
3. forget-customer with retain_financial=True anonymizes PII correctly
4. forget-customer with retain_financial=False soft-deletes all records
5. GDPR requests list endpoint returns all requests
6. Retention policy GET (defaults) and PUT (upsert) CRUD
7. enforce_retention_policy dry-run reports correctly
8. 45-day deadline alert: overdue request marked correctly

All tests use isolated in-memory SQLite DBs — no Postgres or Stripe calls.

SKIPPED: The gdx_dispatch.core.gdpr_router module (with ForgetCustomerRequest,
GDPRRequest, RetentionPolicy, gdpr_forget_customer, gdpr_tenant_export, etc.)
was planned but never implemented. The actual GDPR router lives at
gdx_dispatch/routers/gdpr.py with a different API surface. These tests need to be
rewritten to target the real router.
"""
from __future__ import annotations

import pytest
from uuid import uuid4

pytest.skip(
    "gdx_dispatch.core.gdpr_router does not exist — tests must be rewritten for gdx_dispatch.routers.gdpr",
    allow_module_level=True,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantModelsBase.metadata.create_all(engine, checkfirst=True)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    GDPRRequest.__table__.create(bind=engine, checkfirst=True)
    RetentionPolicy.__table__.create(bind=engine, checkfirst=True)
    return engine


@pytest.fixture()
def db():
    engine = _make_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _make_customer(db, name="Alice Tester", email="alice@example.com", phone="555-1234") -> Customer:
    c = Customer(name=name, email=email, phone=phone, address="123 Main St", company_id="tenant-test")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_job(db, customer_id, title="Spring Tune-up") -> Job:
    j = Job(customer_id=customer_id, title=title, assigned_to="tech_01", company_id="tenant-test")
    db.add(j)
    db.commit()
    db.refresh(j)
    return j


def _make_invoice(db, job_id, number="INV-001") -> Invoice:
    import secrets
    inv = Invoice(
        customer_id=uuid4(),
        job_id=job_id,
        invoice_number=number,
        public_token=secrets.token_hex(32),
        subtotal=100.0,
        tax_amount=8.0,
        total=108.0,
        company_id="tenant-test",
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


# ---------------------------------------------------------------------------
# Test 1: Full export creates valid ZIP with 5 CSV files
# ---------------------------------------------------------------------------

def test_export_creates_zip_with_five_csvs(db):
    """GET /api/gdpr/export returns a StreamingResponse ZIP with 5 CSV members."""
    _make_customer(db)
    job = _make_job(db, db.execute(select(Customer)).scalar_one().id)
    _make_invoice(db, job.id)

    response = gdpr_tenant_export(db=db)

    assert response.media_type == "application/zip"
    assert "attachment" in response.headers.get("Content-Disposition", "")

    # gdpr_tenant_export passes an io.BytesIO directly as the content argument.
    # Extract the underlying BytesIO from the StreamingResponse body_iterator.
    # Starlette wraps a raw BytesIO in an iterate_in_threadpool iterator; the
    # original BytesIO object is accessible via the response's body attribute
    # when using TestClient, but in a unit test we can retrieve it from the
    # iterator's wrapped object or just re-read from the iterator synchronously.
    import asyncio

    async def _collect_body(resp) -> bytes:
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(_collect_body(response))
    zf = zipfile.ZipFile(io.BytesIO(body))
    names = zf.namelist()
    assert "customers.csv" in names
    assert "jobs.csv" in names
    assert "invoices.csv" in names
    assert "audit_log.csv" in names
    assert "technicians.csv" in names

    # customers.csv must have at least the header + 1 data row
    customers_csv = zf.read("customers.csv").decode()
    lines = [l for l in customers_csv.splitlines() if l.strip()]  # noqa: E741
    assert len(lines) >= 2, "Expected header + at least 1 customer row"

    # technicians.csv must contain tech_01
    tech_csv = zf.read("technicians.csv").decode()
    assert "tech_01" in tech_csv


# ---------------------------------------------------------------------------
# Test 2: Export is rate-limited to 1 per 24 hours
# ---------------------------------------------------------------------------

def test_export_rate_limited(db):
    """Second export within 24h raises HTTP 429."""
    # First call succeeds
    gdpr_tenant_export(db=db)

    # Second call within 24h should be rejected
    with pytest.raises(HTTPException) as exc_info:
        gdpr_tenant_export(db=db)
    assert exc_info.value.status_code == 429
    assert "24h" in exc_info.value.detail.lower() or "rate" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Test 3: forget-customer with retain_financial=True anonymizes PII
# ---------------------------------------------------------------------------

def test_forget_customer_retain_financial_anonymizes_pii(db):
    """retain_financial=True replaces PII fields with GDPR_REMOVED."""
    customer = _make_customer(db)
    job = _make_job(db, customer.id)
    inv = _make_invoice(db, job.id)

    body = ForgetCustomerRequest(customer_id=str(customer.id), retain_financial=True)
    result = gdpr_forget_customer(body=body, db=db)

    assert result["ok"] is True
    assert result["retain_financial"] is True

    db.expire_all()
    c = db.execute(select(Customer).where(Customer.id == customer.id)).scalar_one()
    assert c.name == "GDPR_REMOVED"
    assert c.email == "GDPR_REMOVED"
    assert c.phone == "GDPR_REMOVED"
    assert c.address == "GDPR_REMOVED"
    assert c.name_hash is None
    assert c.email_hash is None
    assert c.phone_hash is None
    # Customer record still exists (not soft-deleted)
    assert c.deleted_at is None

    # Job and invoice must NOT be soft-deleted
    j = db.execute(select(Job).where(Job.id == job.id)).scalar_one()
    assert j.deleted_at is None
    i = db.execute(select(Invoice).where(Invoice.id == inv.id)).scalar_one()
    assert i.deleted_at is None


# ---------------------------------------------------------------------------
# Test 4: forget-customer with retain_financial=False soft-deletes all records
# ---------------------------------------------------------------------------

def test_forget_customer_no_retain_soft_deletes_all(db):
    """retain_financial=False soft-deletes customer, jobs, and invoices."""
    customer = _make_customer(db)
    job = _make_job(db, customer.id)
    inv = _make_invoice(db, job.id)

    body = ForgetCustomerRequest(customer_id=str(customer.id), retain_financial=False)
    result = gdpr_forget_customer(body=body, db=db)

    assert result["ok"] is True
    assert result["retain_financial"] is False

    db.expire_all()
    c = db.execute(select(Customer).where(Customer.id == customer.id)).scalar_one()
    assert c.deleted_at is not None

    j = db.execute(select(Job).where(Job.id == job.id)).scalar_one()
    assert j.deleted_at is not None

    i = db.execute(select(Invoice).where(Invoice.id == inv.id)).scalar_one()
    assert i.deleted_at is not None

    # A GDPRRequest record must have been created
    req = db.execute(
        select(GDPRRequest).where(GDPRRequest.customer_id == customer.id)
    ).scalar_one()
    assert req.request_type == "forget"
    assert req.status == "completed"
    assert req.retain_financial is False


# ---------------------------------------------------------------------------
# Test 5: GDPR requests list returns all records
# ---------------------------------------------------------------------------

def test_gdpr_requests_list(db):
    """GET /api/gdpr/requests returns all GDPRRequest rows ordered newest first."""
    customer = _make_customer(db)
    cid = customer.id

    # Create two requests manually
    now = utcnow()
    db.add(GDPRRequest(customer_id=cid, request_type="forget", status="completed",
                       completed_at=now, deadline_at=now + timedelta(days=45)))
    db.add(GDPRRequest(request_type="portability", status="completed",
                       completed_at=now, deadline_at=now + timedelta(days=45)))
    db.commit()

    result = gdpr_list_requests(db=db)

    assert isinstance(result, list)
    assert len(result) == 2

    types = {r["request_type"] for r in result}
    assert "forget" in types
    assert "portability" in types

    for r in result:
        assert "id" in r
        assert "status" in r
        assert "requested_at" in r


# ---------------------------------------------------------------------------
# Test 6: Retention policy CRUD (GET defaults, PUT upsert)
# ---------------------------------------------------------------------------

def test_retention_policy_crud(db):
    """GET returns defaults when no policy exists; PUT creates and then updates."""
    # GET with no record → defaults
    policy = gdpr_get_retention_policy(db=db)
    assert policy["customer_data_days"] == 1825
    assert policy["audit_log_days"] == 2555
    assert policy.get("updated_at") is None

    # PUT creates new record
    body = RetentionPolicyRequest(customer_data_days=730, audit_log_days=1095)
    created = gdpr_set_retention_policy(body=body, db=db)
    assert created["customer_data_days"] == 730
    assert created["audit_log_days"] == 1095
    assert "id" in created

    # GET now returns stored record
    fetched = gdpr_get_retention_policy(db=db)
    assert fetched["customer_data_days"] == 730
    assert fetched["audit_log_days"] == 1095

    # PUT again updates existing record
    body2 = RetentionPolicyRequest(customer_data_days=365, audit_log_days=730)
    updated = gdpr_set_retention_policy(body=body2, db=db)
    assert updated["customer_data_days"] == 365
    assert updated["audit_log_days"] == 730

    # Only one record should exist
    count = len(db.execute(select(RetentionPolicy)).scalars().all())
    assert count == 1


# ---------------------------------------------------------------------------
# Test 7: enforce_retention_policy dry-run reports correctly
# ---------------------------------------------------------------------------

def test_retention_enforcement_dry_run(db):
    """enforce_retention_policy dry_run=True reports deletions without committing."""
    # Create a customer soft-deleted well past the retention window
    c = Customer(name="Old Customer", deleted_at=utcnow() - timedelta(days=2000), company_id="tenant-test")
    db.add(c)
    db.commit()

    result = enforce_retention_policy(db, dry_run=True)

    assert result["dry_run"] is True
    assert str(c.id) in result["deleted_customer_ids"]
    assert result["customers_hard_deleted"] == 1

    # Dry run must NOT actually delete — customer should still exist
    db.expire_all()
    still_there = db.execute(select(Customer).where(Customer.id == c.id)).scalar_one_or_none()
    assert still_there is not None, "Dry run should not commit deletions"


# ---------------------------------------------------------------------------
# Test 8: 45-day deadline alert — overdue request is marked correctly
# ---------------------------------------------------------------------------

def test_45_day_deadline_alert(db):
    """GDPRRequest records past deadline_at are marked 'overdue' by enforce_retention_policy."""
    # Create a pending request with a deadline already passed
    past_deadline = utcnow() - timedelta(days=50)
    req = GDPRRequest(
        request_type="forget",
        status="pending",
        deadline_at=past_deadline,
    )
    db.add(req)
    db.commit()
    req_id = req.id

    # Run enforcement (not dry_run so it commits)
    result = enforce_retention_policy(db, dry_run=False)

    assert str(req_id) in result["overdue_request_ids"]
    assert result["overdue_requests_marked"] >= 1

    db.expire_all()
    updated_req = db.execute(select(GDPRRequest).where(GDPRRequest.id == req_id)).scalar_one()
    assert updated_req.status == "overdue"

    # An audit log entry must have been created for the overdue event
    audit_rows = db.execute(
        select(AuditLog).where(AuditLog.event_type == "gdpr_request_overdue")
    ).scalars().all()
    assert len(audit_rows) >= 1
    assert audit_rows[0].entity_id == str(req_id)
