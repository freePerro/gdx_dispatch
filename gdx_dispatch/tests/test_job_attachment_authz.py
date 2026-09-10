"""Object-level authz on routes that attach things to a CALLER-SUPPLIED job id.

#518: `POST /api/documents` was gated only by `require_module("documents")` +
auth. `require_module` asks whether a feature is enabled for the tenant, never
who the caller is — so any authenticated technician could post
`job_id=<someone else's job>` with `as_photo=true` and mint a `job_photos` row
on it. Reproduced on a running container: 201, `job_photos` 0 -> 1, with
`uploaded_by` set to that user, while `GET /api/mobile/my-jobs/{id}` 404'd the
same user on the same job. The write was more permissive than the read.

`POST /api/mobile/voice-note` is the same shape one file over, found by this
issue's sibling sweep: `job_id` from the multipart form, no gate, and it writes
a transcribed note onto the job.

The three grants that must keep working are the surfaces that legitimately post
here: dispatch/admin, the assigned technician, and the creator of a job that is
still unassigned (the mobile flow where a tech makes a job in the dialog and
photographs it before dispatch assigns it).
"""
from __future__ import annotations

import io
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Job, Technician
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"

OWNER_USER, OWNER_TECH = "user-owner", "tech-owner"
OTHER_USER, OTHER_TECH = "user-other", "tech-other"


def _now() -> datetime:
    return datetime.now(UTC)


def _png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a"
        "0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000100"
        "0d0a2db40000000049454e44ae426082"
    )


@pytest.fixture(autouse=True)
def _upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))


@pytest.fixture
def ctx(monkeypatch):
    """App with BOTH attachment routes mounted and a swappable caller."""
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.core.modules import require_module
    from gdx_dispatch.routers import documents as documents_router
    from gdx_dispatch.routers import voice as voice_router
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user

    caller: dict = {}

    app = FastAPI()
    app.include_router(documents_router.router)
    app.include_router(voice_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller
    app.dependency_overrides[require_module("documents")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = caller
        return await call_next(request)

    # Two technicians. The job below belongs to OWNER.
    db.add(Technician(id=OWNER_TECH, company_id=TENANT, user_id=OWNER_USER, active=True))
    db.add(Technician(id=OTHER_TECH, company_id=TENANT, user_id=OTHER_USER, active=True))
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Fix",
              description="", scheduled_at=_now(), assigned_to=OWNER_TECH)
    db.add(job)
    db.commit()

    def be(user_id: str, role: str = "technician") -> None:
        caller.clear()
        caller.update({"user_id": user_id, "sub": user_id,
                       "tenant_id": TENANT, "role": role})

    yield TestClient(app), db, job, be, cust
    db.close()
    engine.dispose()


def _post_photo(client, job_id) -> int:
    return client.post(
        "/api/documents",
        files={"file": ("door.png", io.BytesIO(_png()), "image/png")},
        data={"job_id": str(job_id), "as_photo": "true"},
    ).status_code


def _photo_rows(db, job_id) -> int:
    """Count job_photos rows, matching BOTH textual uuid forms.

    Postgres renders a uuid dashed; SQLite stores the same value as 32 hex
    characters. A single-form comparison here counts zero on one engine and
    would make these assertions pass or fail for the wrong reason — the same
    trap that made the shared ownership gate unable to match under SQLite.
    """
    from sqlalchemy import text

    raw = str(job_id)
    return db.execute(
        text("SELECT count(*) FROM job_photos WHERE CAST(job_id AS TEXT) IN (:j, :jh)"),
        {"j": raw, "jh": raw.replace("-", "").lower()},
    ).scalar() or 0


class TestDocumentUploadJobGate:
    def test_a_tech_cannot_attach_a_photo_to_someone_elses_job(self, ctx):
        """THE #518 regression. Revert the gate and this goes red."""
        client, db, job, be, _ = ctx
        be(OTHER_USER)
        assert _post_photo(client, job.id) == 404
        # …and nothing was written. A refusal that still left a row would be
        # the same defect with a different status code.
        assert _photo_rows(db, job.id) == 0

    def test_the_refusal_is_404_not_403(self, ctx):
        """403 would confirm the job id exists, letting one tech enumerate
        another's jobs by watching status codes — the reason every mobile read
        gate answers 404."""
        client, _db, job, be, _ = ctx
        be(OTHER_USER)
        assert _post_photo(client, job.id) == 404
        assert _post_photo(client, uuid4()) == 404  # nonexistent id: same answer

    def test_the_assigned_tech_still_can(self, ctx):
        """The mobile photo queue posts here — this must keep working."""
        client, db, job, be, _ = ctx
        be(OWNER_USER)
        assert _post_photo(client, job.id) == 201
        assert _photo_rows(db, job.id) == 1

    def test_dispatch_and_admin_still_can(self, ctx):
        client, db, job, be, _ = ctx
        for role in ("dispatcher", "admin", "owner"):
            be(f"office-{role}", role=role)
            assert _post_photo(client, job.id) == 201, role
        assert _photo_rows(db, job.id) == 3

    def test_the_creator_of_a_still_unassigned_job_can(self, ctx):
        """A tech creates a job in the mobile dialog and photographs it before
        dispatch assigns it. Dropping this grant would break that flow."""
        client, db, _job, be, cust = ctx
        mine = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="New",
                   description="", scheduled_at=_now(), assigned_to=None,
                   created_by=OTHER_USER)
        db.add(mine)
        db.commit()
        be(OTHER_USER)
        assert _post_photo(client, mine.id) == 201

    def test_the_creator_grant_dies_when_the_job_is_assigned_elsewhere(self, ctx):
        """The grant is deliberately narrow: it must not survive dispatch
        handing the job to somebody else."""
        client, db, _job, be, cust = ctx
        mine = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="New",
                   description="", scheduled_at=_now(), assigned_to=None,
                   created_by=OTHER_USER)
        db.add(mine)
        db.commit()
        be(OTHER_USER)
        assert _post_photo(client, mine.id) == 201

        mine.assigned_to = OWNER_TECH          # dispatch reassigns it
        db.commit()
        assert _post_photo(client, mine.id) == 404

    def test_an_upload_with_no_job_id_is_untouched(self, ctx):
        """The gate is scoped to job attachment. A plain document upload has no
        job to be authorized against and must not start failing."""
        client, _db, _job, be, _ = ctx
        be(OTHER_USER)
        r = client.post(
            "/api/documents",
            files={"file": ("spec.png", io.BytesIO(_png()), "image/png")},
        )
        assert r.status_code == 201

    def test_a_refused_upload_writes_no_file(self, ctx, tmp_path):
        """Checked before the bytes are written, so a refusal leaves nothing on
        disk and no Document row — not just no job_photos link."""
        from gdx_dispatch.models.tenant_models import Document

        client, db, job, be, _ = ctx
        before = db.query(Document).count()
        be(OTHER_USER)
        assert _post_photo(client, job.id) == 404
        assert db.query(Document).count() == before


class TestVoiceNoteJobGate:
    """Sibling found by #518's sweep: same shape, same file-supplied job_id."""

    def _post(self, client, job_id) -> int:
        return client.post(
            "/api/mobile/voice-note",
            files={"file": ("note.webm", io.BytesIO(b"audio-bytes"), "audio/webm")},
            data={"job_id": str(job_id)},
        ).status_code

    def test_a_tech_cannot_attach_a_voice_note_to_someone_elses_job(self, ctx):
        client, _db, job, be, _ = ctx
        be(OTHER_USER)
        assert self._post(client, job.id) == 404

    def test_the_assigned_tech_still_can(self, ctx):
        """The gate must let the owner through, and say so positively.

        An earlier version asserted only `!= 404`, which cannot fail for an
        over-refusal that answers 403 — a test that passes for the wrong reason
        is worse than none. With no OPENAI_API_KEY the route still returns 200
        (transcription degrades, the note is written), so assert that.
        """
        from gdx_dispatch.models.tenant_models import JobNote

        client, db, job, be, _ = ctx
        before = db.query(JobNote).count()
        be(OWNER_USER)
        assert self._post(client, job.id) == 200
        assert db.query(JobNote).count() == before + 1

    def test_dispatch_still_can(self, ctx):
        client, _db, job, be, _ = ctx
        be("office-1", role="dispatcher")
        assert self._post(client, job.id) == 200


class TestWhichRolesMayAttach:
    """Pin the answer for every builtin role, so a change here is deliberate.

    This is a role-model question, not an oversight: `viewer` is a read-only
    auditor, `accounting` holds no jobs.* permission, and `sales` holds
    jobs.read_all but not jobs.write. All three are refused. `sales` can reach
    the /documents "Link to Job" dialog, so that button 404s for them — whether
    it should is a product call, recorded rather than guessed at.
    """

    @pytest.mark.parametrize("role", ["owner", "admin", "dispatcher"])
    def test_office_write_tier_may_attach(self, ctx, role):
        client, _db, job, be, _ = ctx
        be(f"u-{role}", role=role)
        assert _post_photo(client, job.id) == 201

    @pytest.mark.parametrize("role", ["sales", "accounting", "viewer"])
    def test_the_read_tier_is_refused(self, ctx, role):
        client, _db, job, be, _ = ctx
        be(f"u-{role}", role=role)
        assert _post_photo(client, job.id) == 404


class TestSweptSiblings:
    """The other writes that attached a record to a caller-supplied job id.

    Same defect shape, not the same file shape: `require_module` asks whether a
    feature is enabled for the tenant, never who the caller is. Each of these
    returned 2xx for a technician with no claim on the job.
    """

    def _mount(self, ctx, router_module, *module_names):
        client, db, job, be, cust = ctx
        return client, db, job, be, cust

    def test_checklist_diagnosis_and_tags_refuse_an_unrelated_tech(self, ctx):
        """Driven through the real routers, mounted alongside the others."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from gdx_dispatch.core.auth import get_current_user as core_gcu
        from gdx_dispatch.core.modules import require_module
        from gdx_dispatch.routers import checklists as checklists_router
        from gdx_dispatch.routers import job_diagnosis as diagnosis_router
        from gdx_dispatch.routers import tags as tags_router
        from gdx_dispatch.routers.auth import get_current_user as routers_gcu

        _c, db, job, _be, _cust = ctx
        caller: dict = {}
        app = FastAPI()
        app.include_router(checklists_router.router)
        app.include_router(diagnosis_router.router)
        app.include_router(tags_router.router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[core_gcu] = lambda: caller
        app.dependency_overrides[routers_gcu] = lambda: caller
        # tags.py picks its gate at import time (`core` if present, else `jobs`)
        # — ask the module rather than hardcoding a key that may not exist.
        for m in {"jobs", tags_router._MODULE_GATE}:
            app.dependency_overrides[require_module(m)] = lambda: True

        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.tenant = {"id": TENANT, "slug": "test"}
            request.state.tenant_id = TENANT
            request.state.user = caller
            return await call_next(request)

        client = TestClient(app)
        caller.update({"user_id": OTHER_USER, "sub": OTHER_USER,
                       "tenant_id": TENANT, "role": "technician"})

        j = str(job.id)

        def refused(r, what):
            """404 AND the gate's own detail.

            Status alone is not enough: with the gate removed,
            POST /checklist still 404s — on the missing template — so an
            assertion on the number passes for the wrong reason. Only the
            detail distinguishes "you may not touch this job" from
            "that template does not exist".
            """
            assert r.status_code == 404, f"{what}: {r.status_code}"
            assert r.json().get("detail") == "Job not found", f"{what}: {r.json()}"

        refused(client.post(f"/api/jobs/{j}/checklist",
                            json={"template_id": str(uuid4())}), "checklist")
        refused(client.post(f"/api/jobs/{j}/diagnosis",
                            json={"service_type": "garage_door_repair",
                                  "answers": {}}), "diagnosis")
        refused(client.post(f"/api/jobs/{j}/tags",
                            json={"tag_id": str(uuid4())}), "tag assign")
        refused(client.delete(f"/api/jobs/{j}/tags/{uuid4()}"), "tag unassign")


class TestTheDoorOut:
    """Gating create and leaving destroy open is half a fix.

    An unrelated technician deleting the photo is the same harm as planting
    one — arguably worse, since it removes the evidence rather than adding to
    it. These pin the delete/update side. Each resolves the target's OWN job
    from the stored row, never from caller input.
    """

    def test_an_unrelated_tech_cannot_delete_a_job_document(self, ctx):
        client, db, job, be, _ = ctx
        be(OWNER_USER)
        assert _post_photo(client, job.id) == 201
        from gdx_dispatch.models.tenant_models import Document
        doc = db.query(Document).filter(Document.job_id.isnot(None)).first()
        assert doc is not None

        be(OTHER_USER)
        r = client.delete(f"/api/documents/{doc.id}")
        assert r.status_code == 404
        assert r.json().get("detail") == "Job not found"
        db.refresh(doc)
        assert doc.deleted_at is None, "the row was soft-deleted despite the refusal"

        # …and the owner still can.
        be(OWNER_USER)
        assert client.delete(f"/api/documents/{doc.id}").status_code == 200

    def test_a_document_with_no_job_is_unaffected(self, ctx):
        """The gate is scoped to job-attached documents; a plain document has no
        job to authorize against and must not become undeletable."""
        from gdx_dispatch.models.tenant_models import Document

        client, db, _job, be, _ = ctx
        be(OTHER_USER)
        r = client.post("/api/documents",
                        files={"file": ("spec.png", io.BytesIO(_png()), "image/png")})
        assert r.status_code == 201
        doc = db.query(Document).filter(Document.job_id.is_(None)).first()
        assert client.delete(f"/api/documents/{doc.id}").status_code == 200


class TestPrimaryPhotoRoute:
    """`POST /api/jobs/{job_id}/photos` in uploads.py — the route the Photos
    page and the phone actually use. `/api/documents?as_photo=true` is the
    secondary one. It carried no job check at all, so #518's stated harm was
    reachable through the front door while the back door was being fixed.
    """

    def _mount(self, db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from gdx_dispatch.core.auth import get_current_user as core_gcu
        from gdx_dispatch.core.modules import require_module
        from gdx_dispatch.routers import uploads as uploads_router
        from gdx_dispatch.routers.auth import get_current_user as routers_gcu

        caller: dict = {}
        app = FastAPI()
        app.include_router(uploads_router.router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[core_gcu] = lambda: caller
        app.dependency_overrides[routers_gcu] = lambda: caller
        app.dependency_overrides[require_module("documents")] = lambda: True

        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.tenant = {"id": TENANT, "slug": "test"}
            request.state.tenant_id = TENANT
            request.state.user = caller
            return await call_next(request)

        return TestClient(app), caller

    def test_an_unrelated_tech_cannot_upload_a_photo_or_a_signature(self, ctx):
        _c, db, job, _be, _ = ctx
        client, caller = self._mount(db)
        caller.update({"user_id": OTHER_USER, "sub": OTHER_USER,
                       "tenant_id": TENANT, "role": "technician"})
        j = str(job.id)

        r = client.post(f"/api/jobs/{j}/photos",
                        files={"file": ("d.png", io.BytesIO(_png()), "image/png")})
        assert r.status_code == 404, r.text
        assert _photo_rows(db, job.id) == 0

        import base64
        r = client.post(f"/api/jobs/{j}/signature",
                        json={"signature": base64.b64encode(_png()).decode()})
        assert r.status_code == 404, r.text

    def test_the_assigned_tech_still_can(self, ctx):
        _c, db, job, _be, _ = ctx
        client, caller = self._mount(db)
        caller.update({"user_id": OWNER_USER, "sub": OWNER_USER,
                       "tenant_id": TENANT, "role": "technician"})
        r = client.post(f"/api/jobs/{job.id}/photos",
                        files={"file": ("d.png", io.BytesIO(_png()), "image/png")})
        assert r.status_code == 201, r.text
        assert _photo_rows(db, job.id) == 1


class TestMalformedJobId:
    """A phone posting `job_id=undefined` must 404, not 500.

    On Postgres `jobs.id` is a uuid column, so raw client text reaching
    `j.id = :j` raises InvalidTextRepresentation and aborts the caller's
    transaction. The SQLite harness cannot see that — the same input is a
    benign no-match there — so this pins the PARSE, which is what makes both
    engines agree.
    """

    @pytest.mark.parametrize(
        "bad", ["undefined", "null", "not-a-uuid", "1234", "'; DROP TABLE jobs;--"]
    )
    def test_a_malformed_job_id_is_refused_not_a_crash(self, ctx, bad):
        client, _db, _job, be, _ = ctx
        be(OWNER_USER)
        assert _post_photo(client, bad) in (404, 422), bad

    def test_an_empty_job_id_means_no_job_not_a_bad_one(self, ctx):
        """Deliberately NOT in the list above. An empty form field is how the
        client says "this document is not attached to a job" — the route
        normalizes it to None and the gate never runs. 201 is correct; refusing
        it would break every plain document upload."""
        client, db, _job, be, _ = ctx
        from gdx_dispatch.models.tenant_models import Document

        be(OWNER_USER)
        assert _post_photo(client, "") == 201
        assert db.query(Document).filter(Document.job_id.is_(None)).count() >= 1

    def test_a_non_uuid_id_skips_the_native_comparison_but_still_matches(self):
        """The parse decides the SQL SHAPE, it does not reject the id.

        Plenty of job ids in this codebase are plain strings ("job-1"), and an
        earlier version of this guard refused them outright — an ownership gate
        that answers "no" for a legitimate owner. The uuid parse exists only to
        decide whether the native `= :j_native` branch (which Postgres would
        raise on for non-uuid text) is safe to include.
        """
        from gdx_dispatch.core.job_access import _job_id_params

        mode, params = _job_id_params("job-1")
        assert mode == "text"
        assert "j_native" not in params          # never handed to a uuid column
        assert params["j"] == "job-1"            # …but still matched as text

        mode, params = _job_id_params("undefined")
        assert mode == "text" and "j_native" not in params

        # A valid uuid in any rendering canonicalises to the same params.
        u = "A1B2C3D4-1111-2222-3333-444455556666"
        assert _job_id_params(u) == _job_id_params(u.lower()) == _job_id_params(u.replace("-", ""))
        mode, params = _job_id_params(u)
        assert mode == "native" and params["j_native"] == u.lower()
