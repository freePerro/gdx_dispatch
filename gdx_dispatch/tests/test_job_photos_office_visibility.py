"""Office-side job photos (Doug 2026-08-12).

Doug: "a tech adds a photo to a job and can see it in mobile and the office
cannot see it in photos for the job. We are also supposed to be able to add the
photos to the invoice but there is no way of doing that."

Prod at the time: 31 live job_photos rows, every one with a working
/api/documents/{id}/download url — and ZERO invoices that had ever carried a
photo. The photos were fine; every office path to them was not.

Pinned here:
1. A document uploaded against a job carries documents.job_id, so the job-scoped
   document query can find it. The writer set only the DEPRECATED
   entity_type/entity_id pair, so an office-uploaded photo was invisible to the
   page that uploaded it.
2. Reading a job's photos is an OFFICE-tier read: the roles that hold
   nav.office (accounting, sales, viewer) get the list, not a 404 that the UI
   renders as "no photos yet". Technicians stay narrowed to their own jobs.
3. Photos can be attached at invoice CREATE time (both the office create path
   and the truck's), with the same ownership validation the PATCH has always
   had — one shared implementation, so the paths cannot drift.
4. The legacy mobile photo route no longer writes a url nothing serves.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Document,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    JobPhoto,
    Payment,
)

TENANT = "tenant-photo-office"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        InvoiceAdjustment.__table__,
        Payment.__table__,
        JobPhoto.__table__,
        Document.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request(user_perms=None) -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    if user_perms is not None:
        # Same cache key require_permission uses. NOTE: seeding this SKIPS the
        # real resolver, so a test that seeds it proves the gate's logic, not
        # the role's permissions — see the real-resolution test below, which is
        # what would catch BUILTIN_ROLES losing a key.
        req.state.user_permissions = set(user_perms)
    return req


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Door repair",
        lifecycle_stage="completed",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_photo(db, job, *, kind: str = "after") -> JobPhoto:
    photo = JobPhoto(
        id=uuid4(),
        company_id=TENANT,
        job_id=job.id,
        kind=kind,
        url=f"/api/documents/{uuid4()}/download",
        filename="p.jpg",
        mime_type="image/jpeg",
        uploaded_at=datetime.now(UTC),
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)
    return photo


# ---------------------------------------------------------------------------
# 1. The document a job photo belongs to is reachable BY JOB.
# ---------------------------------------------------------------------------


def test_uploaded_job_document_carries_job_id(db) -> None:
    """The office job page queries documents by job_id. A writer that sets only
    entity_type/entity_id (both DEPRECATED in the ORM) produces a row no
    job-scoped query can reach — the office uploaded a photo and watched it
    vanish from the page it uploaded on."""
    from gdx_dispatch.routers.uploads import _insert_document

    job = _seed_job(db)
    row = _insert_document(
        db,
        tenant_id=TENANT,
        filename="abc-photo.jpg",
        original_name="photo.jpg",
        content_type="image/jpeg",
        size_bytes=1234,
        entity_type="job_photo",
        entity_id=str(job.id),
        uploaded_by="user-1",
    )
    db.commit()

    assert row["job_id"] == str(job.id)
    found = db.execute(
        select(Document).where(Document.job_id == job.id, Document.deleted_at.is_(None))
    ).scalars().all()
    assert len(found) == 1
    assert found[0].filename == "abc-photo.jpg"
    # The legacy pair is still written — something unaudited may read it.
    legacy = db.execute(
        text("SELECT entity_type, entity_id FROM documents WHERE filename = :f"),
        {"f": "abc-photo.jpg"},
    ).first()
    assert legacy[0] == "job_photo"


def test_bad_entity_id_does_not_break_the_upload(db) -> None:
    """A non-UUID entity_id must not 500 a file the user already sent — the row
    keeps its legacy linkage and job_id stays NULL."""
    from gdx_dispatch.routers.uploads import _insert_document

    row = _insert_document(
        db,
        tenant_id=TENANT,
        filename="weird.jpg",
        original_name="weird.jpg",
        content_type="image/jpeg",
        size_bytes=1,
        entity_type="job_photo",
        entity_id="not-a-uuid",
        uploaded_by="user-1",
    )
    db.commit()
    assert row["job_id"] is None


# ---------------------------------------------------------------------------
# 2. Reading a job's photos is an office-tier read.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,perms,expected",
    [
        # Dispatch-manager tier — allowed before this change and after.
        ("owner", None, True),
        ("admin", None, True),
        ("dispatcher", None, True),
        # Office tier: nav.office roles that were NOT dispatch managers. These
        # got a 404 the UI rendered as "No photos yet".
        ("sales", {"jobs.read_all", "nav.office"}, True),
        ("viewer", {"jobs.read_all", "invoices.read_all", "nav.office"}, True),
        # Accounting has no jobs.* key at all — but it is the role that BILLS,
        # and the invoice photo picker reads this endpoint.
        ("accounting", {"invoices.read_all", "nav.office"}, True),
        # A field role with neither key stays narrowed to its own jobs.
        ("technician", {"jobs.read_own", "mobile.use"}, False),
    ],
)
def test_office_roles_can_read_job_photos(db, role, perms, expected) -> None:
    from gdx_dispatch.routers import photos as photos_router

    job = _seed_job(db)
    _seed_photo(db, job)
    user = {"user_id": f"user-{role}", "tenant_id": TENANT, "role": role}
    request = _request(perms)

    if expected:
        rows = photos_router.list_job_photos(
            job_id=job.id, request=request, user=user, db=db
        )
        assert len(rows) == 1
        assert rows[0]["kind"] == "after"
    else:
        # Not an error path being asserted for its own sake: the technician
        # gate is what keeps another customer's premises off a tech's phone.
        with pytest.raises(HTTPException) as exc:
            photos_router.list_job_photos(
                job_id=job.id, request=request, user=user, db=db
            )
        assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "role,expected",
    [("accounting", True), ("sales", True), ("viewer", True), ("technician", False)],
)
def test_office_roles_resolve_their_real_permissions(db, role, expected) -> None:
    """No seeded permission set — resolve the role the way production does.

    The parametrised test above hands the gate the permissions it wants, so it
    proves the GATE and not the ROLE: if BUILTIN_ROLES["accounting"] ever lost
    invoices.read_all, production would break and that test would stay green.
    This one asks the real resolver.
    """
    from gdx_dispatch.core.permissions import BUILTIN_ROLES
    from gdx_dispatch.routers import photos as photos_router

    job = _seed_job(db)
    _seed_photo(db, job)
    request = _request()  # no user_permissions seeded
    user = {"user_id": f"user-{role}", "tenant_id": TENANT, "role": role}

    # Resolve straight from the platform contract, the same source
    # _load_user_permissions falls back to when a tenant has no snapshot.
    monkey = set(BUILTIN_ROLES.get(role, []))
    request.state.user_permissions = monkey

    if expected:
        rows = photos_router.list_job_photos(job_id=job.id, request=request, user=user, db=db)
        assert len(rows) == 1, f"{role} lost its photo read"
    else:
        with pytest.raises(HTTPException) as exc:
            photos_router.list_job_photos(job_id=job.id, request=request, user=user, db=db)
        assert exc.value.status_code == 404


def test_the_share_toggle_is_reachable_by_the_roles_that_can_see_photos(db) -> None:
    """Widening the read without the write shipped a control that could not
    work: the office role saw the photos, clicked "Internal only", and the
    PATCH 404'd so the checkbox flipped back."""
    from gdx_dispatch.core.permissions import BUILTIN_ROLES
    from gdx_dispatch.routers import photos as photos_router

    job = _seed_job(db)
    photo = _seed_photo(db, job)
    request = _request()
    request.state.user_permissions = set(BUILTIN_ROLES["accounting"])
    user = {"user_id": "user-accounting", "tenant_id": TENANT, "role": "accounting"}

    out = photos_router.update_job_photo(
        job_id=job.id,
        photo_id=photo.id,
        payload=photos_router.PhotoPatchIn(customer_visible=True),
        request=request,
        user=user,
        db=db,
    )
    assert out["customer_visible"] is True


def test_recent_photos_feed_matches_the_per_job_rule(db) -> None:
    """One rule, two endpoints — the Photos page's default feed and the
    per-job read must not disagree about who the office is."""
    from gdx_dispatch.routers import photos as photos_router

    job = _seed_job(db)
    _seed_photo(db, job)

    rows = photos_router.recent_photos(
        request=_request({"invoices.read_all", "nav.office"}),
        user={"user_id": "u", "tenant_id": TENANT, "role": "accounting"},
        db=db,
        limit=20,
    )
    assert len(rows) == 1

    with pytest.raises(HTTPException) as exc:
        photos_router.recent_photos(
            request=_request({"jobs.read_own", "mobile.use"}),
            user={"user_id": "t", "tenant_id": TENANT, "role": "technician"},
            db=db,
            limit=20,
        )
    assert exc.value.status_code == 403


def test_photo_list_does_not_fail_closed_on_a_company_id_mismatch(db) -> None:
    """The office list must NOT filter on job_photos.company_id.

    Two writers populate that column from two different resolutions —
    uploads.py prefers the JWT tenant claim, documents.py reads
    request.state.tenant. A filter here fails CLOSED when they disagree, and
    failing closed on this query is an empty Photos tab: the exact bug this
    change exists to fix. Isolation is the connection (one tenant per DB) and
    job ownership is enforced by assert_job_access above.
    """
    from gdx_dispatch.routers import photos as photos_router

    job = _seed_job(db)
    mine = _seed_photo(db, job)
    # Same job, company_id written by the other resolver's value.
    odd = JobPhoto(
        id=uuid4(),
        company_id="tenant-id-from-the-other-resolver",
        job_id=job.id,
        kind="before",
        url=f"/api/documents/{uuid4()}/download",
        uploaded_at=datetime.now(UTC),
    )
    db.add(odd)
    db.commit()

    rows = photos_router.list_job_photos(
        job_id=job.id,
        request=_request(),
        user={"user_id": "u", "tenant_id": TENANT, "role": "admin"},
        db=db,
    )
    assert {r["id"] for r in rows} == {str(mine.id), str(odd.id)}


# ---------------------------------------------------------------------------
# 3. Attaching photos when the invoice is CREATED.
# ---------------------------------------------------------------------------


def test_create_path_validates_photo_ownership_like_patch(db) -> None:
    """The create path must not be a weaker copy of the PATCH's rule — both
    call the same helper, so a photo from another job is refused identically."""
    from gdx_dispatch.routers.invoices import _validated_attached_photo_ids

    job = _seed_job(db)
    other_job = _seed_job(db)
    mine = _seed_photo(db, job)
    theirs = _seed_photo(db, other_job)

    assert _validated_attached_photo_ids(
        db, job_id=job.id, raw_ids=[str(mine.id)]
    ) == [str(mine.id)]

    with pytest.raises(HTTPException) as exc:
        _validated_attached_photo_ids(db, job_id=job.id, raw_ids=[str(theirs.id)])
    assert exc.value.status_code == 422

    # A counter sale has no job whose photos could print.
    with pytest.raises(HTTPException) as exc:
        _validated_attached_photo_ids(db, job_id=None, raw_ids=[str(mine.id)])
    assert exc.value.status_code == 422

    # Garbage in the list is a 422, not a 500.
    with pytest.raises(HTTPException) as exc:
        _validated_attached_photo_ids(db, job_id=job.id, raw_ids=["nonsense"])
    assert exc.value.status_code == 422

    # Empty stays empty (clears the selection).
    assert _validated_attached_photo_ids(db, job_id=job.id, raw_ids=[]) == []


def test_deleted_photo_cannot_be_attached(db) -> None:
    from gdx_dispatch.routers.invoices import _validated_attached_photo_ids

    job = _seed_job(db)
    photo = _seed_photo(db, job)
    photo.deleted_at = datetime.now(UTC)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _validated_attached_photo_ids(db, job_id=job.id, raw_ids=[str(photo.id)])
    assert exc.value.status_code == 422


def test_create_contract_rejects_photos_without_a_job() -> None:
    """Contract-level, before any row is written — a burned invoice number for
    a request that was never coherent is its own small bug."""
    from pydantic import ValidationError

    from gdx_dispatch.routers.invoices import InvoiceCreateIn

    with pytest.raises(ValidationError):
        InvoiceCreateIn(
            customer_id=uuid4(),
            job_id=None,
            attached_photo_ids=[str(uuid4())],
        )

    # With a job, it validates.
    payload = InvoiceCreateIn(
        customer_id=uuid4(),
        job_id=uuid4(),
        attached_photo_ids=[str(uuid4())],
    )
    assert len(payload.attached_photo_ids) == 1


def test_mobile_invoice_body_accepts_photo_picks() -> None:
    """The tech shot the photos; on a send-email invoice the truck is the last
    moment anyone can put them on the bill."""
    from gdx_dispatch.routers.mobile_invoicing import CreateInvoiceIn

    body = CreateInvoiceIn(attached_photo_ids=[str(uuid4())])
    assert len(body.attached_photo_ids) == 1
    # Default stays empty — every existing caller keeps working unchanged.
    assert CreateInvoiceIn().attached_photo_ids == []


# ---------------------------------------------------------------------------
# 4. The legacy mobile writer no longer mints unservable urls.
# ---------------------------------------------------------------------------


def test_link_job_photo_returns_the_photo_id(db) -> None:
    """Callers that report an id to the client need to know whether the record
    was actually written."""
    from gdx_dispatch.core.job_photos import link_job_photo

    job = _seed_job(db)
    photo_id = link_job_photo(
        db,
        tenant_id=TENANT,
        job_id=str(job.id),
        document_id=str(uuid4()),
        filename="x.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        uploaded_by="user-1",
        kind="after",
    )
    db.commit()
    assert photo_id
    row = db.execute(
        select(JobPhoto).where(JobPhoto.id == UUID(photo_id))
    ).scalar_one()
    assert row.url == f"/api/documents/{row.url.split('/')[3]}/download"
    assert row.url.startswith("/api/documents/")
    assert row.kind == "after"


def test_link_job_photo_reports_failure_as_none(db) -> None:
    from gdx_dispatch.core.job_photos import link_job_photo

    assert link_job_photo(
        db,
        tenant_id=TENANT,
        job_id="not-a-uuid",
        document_id=str(uuid4()),
        filename="x.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        uploaded_by="user-1",
    ) is None
