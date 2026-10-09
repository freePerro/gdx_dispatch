"""Object-level authz on every job-scoped WRITE route in routers/jobs.py.

GDXA-32. Until 2026-09-25 ten of the thirteen job-scoped write routes in
`gdx_dispatch/routers/jobs.py` were gated by nothing but `require_module("jobs")`
— tenant feature-enablement — plus being authenticated. `require_module` never
asks who the caller is. So the gates were: has a token, module on. Nothing else.
An eleventh, `delete_job`, carried `require_permission("jobs.write")` and was
listed as already gated; a permission gate is not an object gate, and the
builtin technician role holds `jobs.write`.

Reproduced over HTTP against the real app before the fix, two arms:

    Arm A  technician with NO assignment / appointment / crew row on the job
           GET  /api/jobs/{id}/closeout -> 404 {"detail":"job not found"}
           POST /api/jobs/{id}/closeout -> 201 {"ok":true,...}
           job_closeouts: hours_worked=6.00 closed_by_user_id=<the stranger>

    Arm B  `viewer` — the read-only auditor role whose entire builtin
           permission set is `.read` keys plus nav.office
           POST /api/jobs/{id}/closeout -> 201 Created
           job_closeouts: hours_worked=8.00, job flipped to 'completed'

The read was hardened and the write was not, in one request pair. A closeout is
the tech's attested parts and hours; billed labor comes from attested hours
only, and the handler runs autodraft_invoice_for_closeout — so hours nobody
attested could reach an invoice.

WHY THIS FILE AND NOT `tests/authz_sweep.py`: the sweep counts any
authenticated route as gated, so it was green on every one of these rows
before the fix and stays green after. It cannot fail for this defect. This
file can: delete any one `job_write_denial` call from routers/jobs.py and the
matching `ROUTES` row below goes red.

THREE refusal arms, each driving all eleven routes, because each refuses for a
different reason and a fix can break one while passing the others:

  * TestArmAUnclaimedTechnician      — BUILTIN technician: no claim, no read
  * TestArmAProductionShapedTechnician — PROD's technician, whose role
    snapshot carries `jobs.read_all`. Read the class docstring before
    touching the predicate: the first cut of this fix passed every other test
    here and refused nobody in production.
  * TestArmBReadOnlyViewer          — holds the read, not the write

The admission tests then prove the predicate is not a blanket refusal.
Together that is the whole gate: every route calls it (the refusal arms), and
it admits the callers it must (the admission arms). The call sites pass
identical arguments, so there is no per-route variation left for the
admission tests to cover.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Customer,
    Job,
    JobDependency,
    Technician,
)
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-authz"

# Real uuids: _load_user_permissions looks the caller up in `users` by id, and
# a non-uuid id makes that lookup raise on the Uuid column instead of simply
# missing — a different code path from the one production takes.
ASSIGNED_USER = str(uuid4())
ASSIGNED_TECH = str(uuid4())
STRANGER_USER = str(uuid4())
STRANGER_TECH = str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _routes(job_id: str, dep_id: str) -> list[tuple[str, str, str, dict]]:
    """(label, method, path, json) for every job-scoped write route.

    Eleven rows. GDXA-32 named ten and listed `delete_job` among the three
    "already gated" — it was not. Its decorator asserts `jobs.write`, a key
    the builtin technician role holds, so an unclaimed tech passed it and
    soft-deleted another tech's job. A permission gate is not an object gate;
    that distinction is the entire class. Found by the 2026-09-25 audit of
    this fix, which also caught the first draft of the structural test below
    certifying it as safe.

    POST+DELETE /{job_id}/not-billable stay out: they assert `invoices.write`,
    which no field-tier role holds, so holding it IS the office-tier
    assertion the class asks for. That exemption is itself pinned, by
    TestTheGateCoversTheWholeClass below.
    """
    return [
        ("update_job", "PATCH", f"/api/jobs/{job_id}", {"title": "hijacked"}),
        ("delete_job", "DELETE", f"/api/jobs/{job_id}", {}),
        ("start_job", "POST", f"/api/jobs/{job_id}/start", {}),
        ("complete_job", "POST", f"/api/jobs/{job_id}/complete", {}),
        (
            "close_job_without_work",
            "POST",
            f"/api/jobs/{job_id}/close-without-work",
            {"reason": "duplicate of another job"},
        ),
        (
            "closeout_job",
            "POST",
            f"/api/jobs/{job_id}/closeout",
            {"parts": [], "hours": 6.0, "no_parts_used": True},
        ),
        (
            "add_job_dependency",
            "POST",
            f"/api/jobs/{job_id}/dependencies",
            {"depends_on_job_id": str(uuid4())},
        ),
        (
            "remove_job_dependency",
            "DELETE",
            f"/api/jobs/{job_id}/dependencies/{dep_id}",
            {},
        ),
        ("create_follow_up_job", "POST", f"/api/jobs/{job_id}/follow-up", {}),
        (
            "spawn_return_visit",
            "POST",
            f"/api/jobs/{job_id}/spawn-return-visit",
            {"reason": "warranty callback"},
        ),
        (
            "uncomplete_job",
            "POST",
            f"/api/jobs/{job_id}/uncomplete",
            {"reason": "wrong job closed"},
        ),
        (
            "reactivate_job",
            "POST",
            f"/api/jobs/{job_id}/reactivate",
            {"reason": "customer called back"},
        ),
        # GDXA-375: the twelfth, added with the route.
        (
            "cancel_job",
            "POST",
            f"/api/jobs/{job_id}/cancel",
            {"reason": "customer went elsewhere"},
        ),
    ]


ROUTES = [r[0] for r in _routes("j", "d")]


@pytest.fixture
def ctx(monkeypatch):
    """Real app, real router, real permission resolution, swappable caller.

    `get_current_user` is overridden (there is no login server here) but
    NOTHING about the gate is: permissions resolve through the ordinary
    core.modules._load_user_permissions path off the caller's role, and the
    module grant is a real `company_module_grants` row rather than an
    override, so `require_module("jobs")` genuinely passes.
    """
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    db.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"grant-{TENANT}", "tid": TENANT},
    )
    db.commit()

    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user
    from gdx_dispatch.routers.jobs import router as jobs_router

    caller: dict = {}

    app = FastAPI()
    app.include_router(jobs_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "authz"}
        request.state.tenant_id = TENANT
        request.state.user = caller
        return await call_next(request)

    db.add(Technician(id=ASSIGNED_TECH, company_id=TENANT,
                      user_id=ASSIGNED_USER, active=True))
    db.add(Technician(id=STRANGER_TECH, company_id=TENANT,
                      user_id=STRANGER_USER, active=True))
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    db.flush()
    # Completed, so /uncomplete has something to revert; assigned to the
    # technician above, so the claim half of the gate has a TRUE branch.
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Fix",
              description="", scheduled_at=_now(), assigned_to=ASSIGNED_TECH,
              lifecycle_stage="completed", completed_at=_now())
    db.add(job)
    dep = JobDependency(id=str(uuid4()), tenant_id=TENANT, job_id=str(job.id),
                        depends_on_job_id=str(uuid4()),
                        created_at=_now().isoformat())
    db.add(dep)
    db.commit()

    def be(user_id: str, role: str) -> None:
        caller.clear()
        caller.update({"user_id": user_id, "sub": user_id,
                       "tenant_id": TENANT, "role": role})

    yield TestClient(app, raise_server_exceptions=False), db, job, dep, be, cust
    db.close()
    engine.dispose()


def _call(client, method: str, path: str, body: dict):
    return client.request(method, path, json=body)


def _by_label(job, dep, label: str):
    for lbl, method, path, body in _routes(str(job.id), str(dep.id)):
        if lbl == label:
            return method, path, body
    raise AssertionError(f"unknown route label {label}")


def _job_row(db, job_id):
    """Re-read the job as raw values, matching both textual uuid forms.

    Postgres renders a uuid dashed and SQLite stores it as 32 hex characters;
    a single-form comparison would count zero on one engine and make these
    assertions pass for the wrong reason.
    """
    raw = str(job_id)
    return db.execute(
        text(
            "SELECT lifecycle_stage, title, completed_at FROM jobs "
            "WHERE CAST(id AS TEXT) IN (:j, :jh)"
        ),
        {"j": raw, "jh": raw.replace("-", "").lower()},
    ).first()


class TestArmAUnclaimedTechnician:
    """The stranger tech. Holds jobs.write (builtin technician does) but has
    no assignment, appointment or crew row on this job — so a bare
    `require_permission("jobs.write")` would have admitted them, which is why
    the gate is a conjunction."""

    @pytest.mark.parametrize("label", ROUTES)
    def test_route_refuses(self, ctx, label):
        client, _db, job, dep, be, _ = ctx
        be(STRANGER_USER, "technician")
        method, path, body = _by_label(job, dep, label)
        r = _call(client, method, path, body)
        assert r.status_code == 404, f"{label}: {r.status_code} {r.text[:300]}"
        assert r.json() == {"detail": "job not found"}, label

    def test_nothing_was_written(self, ctx):
        """A refusal that still mutated would be the same defect wearing a
        different status code."""
        client, db, job, dep, be, _ = ctx
        before = _job_row(db, job.id)
        be(STRANGER_USER, "technician")
        for _lbl, method, path, body in _routes(str(job.id), str(dep.id)):
            _call(client, method, path, body)
        db.expire_all()
        assert _job_row(db, job.id) == before
        assert db.execute(
            text("SELECT count(*) FROM job_closeouts")
        ).scalar() == 0
        # No child job was minted off it by /follow-up or /spawn-return-visit.
        assert db.execute(text("SELECT count(*) FROM jobs")).scalar() == 1

    def test_the_refusal_is_404_not_403(self, ctx):
        """403 confirms the id exists and lets one tech enumerate another's
        jobs by watching status codes — the reason every mobile read gate
        answers 404. A nonexistent id must answer identically."""
        client, _db, job, dep, be, _ = ctx
        be(STRANGER_USER, "technician")
        method, path, body = _by_label(job, dep, "closeout_job")
        real = _call(client, method, path, body)
        ghost = _call(client, "POST", f"/api/jobs/{uuid4()}/closeout", body)
        assert real.status_code == ghost.status_code == 404
        assert real.json() == ghost.json()


class TestArmAProductionShapedTechnician:
    """Arm A again, with PRODUCTION's actual technician permissions.

    The first cut of this fix keyed the office-tier pass on ``jobs.read_all``
    and was verified entirely against ``BUILTIN_ROLES["technician"]``, which
    does not hold it. Production's does. Checked on the live database
    2026-09-25: both live technicians are assigned a TenantRole named
    `technician` whose snapshot is

        ["jobs.read_own", "jobs.write", ..., "jobs.read_all",
         "customers.read_all", "customers.write", "invoices.write",
         "invoices.send", "invoices.read_own", "invoices.read_all", ...]

    and ``core.modules._load_user_permissions`` treats that snapshot as
    authoritative for every non-admin/owner role. So the fix would have
    refused nobody on the one installation it exists to protect — green
    tests, live hole. That is what this class exists to stop coming back.

    It seeds a real TenantRole + UserRoleAssignment so permission resolution
    takes the SNAPSHOT path, not the BUILTIN fallback every other test here
    takes.
    """

    @pytest.fixture
    def prod_tech(self, ctx):
        import json

        from gdx_dispatch.models.tenant_models import TenantRole, UserRoleAssignment

        client, db, job, dep, be, _ = ctx
        # Uuid(as_uuid=True) columns: bind real UUID objects, never str —
        # SQLite's Uuid processor calls .hex on the bound value.
        role_id = uuid4()
        db.add(TenantRole(
            id=role_id, company_id=TENANT, name="technician",
            permissions=json.dumps([
                "jobs.read_own", "jobs.write", "scheduling.read_own",
                "customers.read_own", "estimates.read_own", "inventory.read",
                "inventory.write", "pricing.labor_matrix.read", "mobile.use",
                "mobile.chat", "jobs.read_all", "customers.read_all",
                "customers.write", "invoices.write", "invoices.send",
                "invoices.read_own", "invoices.read_all",
                "customers.contact_write",
            ]),
        ))
        db.add(UserRoleAssignment(
            id=uuid4(), company_id=TENANT,
            user_id=STRANGER_USER, role_id=role_id,
        ))
        db.commit()
        return client, db, job, dep, be

    def test_the_snapshot_path_is_really_what_resolves(self, prod_tech):
        """Falsifier for this whole class: if resolution fell back to
        BUILTIN, these tests would pass for the wrong reason — they would be
        re-testing Arm A rather than the production shape. Assert the
        resolved set is the SNAPSHOT (it holds jobs.read_all; BUILTIN does
        not)."""
        from gdx_dispatch.core.modules import _load_user_permissions
        from gdx_dispatch.core.permissions import BUILTIN_ROLES

        _client, db, _job, _dep, be = prod_tech
        be(STRANGER_USER, "technician")

        class _Req:
            class state:
                tenant = {"id": TENANT}
        perms = _load_user_permissions(db, _Req(), {"user_id": STRANGER_USER,
                                                    "sub": STRANGER_USER,
                                                    "role": "technician"})
        assert "jobs.read_all" in perms, perms
        assert "jobs.read_all" not in BUILTIN_ROLES["technician"]

    @pytest.mark.parametrize("label", ROUTES)
    def test_route_still_refuses(self, prod_tech, label):
        """jobs.write ✓, jobs.read_all ✓, no claim on the job — and still
        refused, because a field technician never gets the blanket
        office-tier pass whatever their snapshot says."""
        client, _db, job, dep, be = prod_tech
        be(STRANGER_USER, "technician")
        method, path, body = _by_label(job, dep, label)
        r = _call(client, method, path, body)
        assert r.status_code == 403, f"{label}: {r.status_code} {r.text[:300]}"

    def test_the_refusal_is_403_because_they_can_read_it(self, prod_tech):
        """403, not 404, and that is deliberate: jobs.read_all means they can
        already list and open this job, so 'not found' would be a lie that
        buys no enumeration protection. The 404 is reserved for a caller who
        cannot see the job at all (Arm A above)."""
        client, _db, job, _dep, be = prod_tech
        be(STRANGER_USER, "technician")
        assert client.get(f"/api/jobs/{job.id}/closeout").status_code == 200
        r = _call(client, "POST", f"/api/jobs/{job.id}/closeout",
                  {"parts": [], "hours": 6.0, "no_parts_used": True})
        assert r.status_code == 403, r.text[:300]

    def test_nothing_was_written(self, prod_tech):
        client, db, job, dep, be = prod_tech
        be(STRANGER_USER, "technician")
        for _lbl, method, path, body in _routes(str(job.id), str(dep.id)):
            _call(client, method, path, body)
        db.expire_all()
        assert db.execute(text("SELECT count(*) FROM job_closeouts")).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM jobs")).scalar() == 1


class TestArmBReadOnlyViewer:
    """`viewer` is the read-only auditor: its entire builtin permission set is
    `.read` keys plus nav.office. It holds `jobs.read_all`, so it can SEE the
    job — only the write half refuses it. That makes it the test that fails if
    someone 'simplifies' the conjunction down to the claim check alone.

    403 rather than 404 for the same reason as the production technician
    above: a caller who can open the job learns nothing from 'not found', and
    an auditor deserves to be told it is a permission problem."""

    @pytest.mark.parametrize("label", ROUTES)
    def test_route_refuses(self, ctx, label):
        client, _db, job, dep, be, _ = ctx
        be(str(uuid4()), "viewer")
        method, path, body = _by_label(job, dep, label)
        r = _call(client, method, path, body)
        assert r.status_code == 403, f"{label}: {r.status_code} {r.text[:300]}"

    @pytest.mark.parametrize("role", ["sales", "accounting"])
    def test_the_other_write_less_office_roles_are_refused_too(self, ctx, role):
        """`sales` holds jobs.read_all and NOT jobs.write; `accounting` holds
        no jobs.* key at all. Neither may write a job."""
        client, _db, job, _dep, be, _ = ctx
        be(str(uuid4()), role)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"title": "no"})
        assert r.status_code in (403, 404), f"{role}: {r.status_code}"

    def test_viewer_really_does_hold_jobs_read_all(self):
        """Names the falsifier for the class above: if `viewer` lost
        jobs.read_all these tests would still pass, but for the wrong
        reason — they would no longer prove the write half does anything."""
        from gdx_dispatch.core.permissions import BUILTIN_ROLES

        assert "jobs.read_all" in BUILTIN_ROLES["viewer"]
        assert "jobs.write" not in BUILTIN_ROLES["viewer"]


class TestAdmission:
    """The other half of a gate: who must still get through — executed, not
    asserted from a mock.

    Driven on /closeout and /start since GDXA-32, and since GDXA-88 on the six
    routes that could not be executed here at all: PATCH and DELETE /{id},
    /uncomplete, /reactivate, /follow-up and /spawn-return-visit. Each of
    those six validated `job_id` as a uuid and then bound the raw path STRING
    to `Job.id` — a `Uuid()` column — which SQLite rejects with
    StatementError ('str' object has no attribute 'hex'); the routers' own
    `except SQLAlchemyError` turned that into their deliberate 500. Postgres
    takes either form, so the shape was invisible in production and surfaced
    only as GDXA-37's complaint, "PATCH /api/jobs/{id} returned 500 (not 403)
    for a technician". It was never an authz decision.

    WHY THAT MATTERS TO THIS FILE, not just to the routers: the refusal arms
    above always passed on those six, because the gate answers before the
    query runs. So a route could be certified refused-correctly and still be
    incapable of serving the caller it admits, and no arm here would notice. A
    refusal proven on a route whose success path cannot execute is half a gate.

    The three remaining ROUTES rows — /complete and the two /dependencies —
    were always executable and stay undriven for the original reason: the call
    sites pass identical arguments to the same predicate, so there is no
    per-route variation left for an admission test to cover.

    WHAT THESE TESTS DELIBERATELY DO NOT ASSERT. On the five routes other than
    PATCH the admitted caller here is the OFFICE tier, which is the caller
    every one of them has a named UI for. Whether a technician should also be
    able to soft-delete, un-complete, reactivate or spawn children off their
    own job is a role-configuration question — they pass today only because
    the builtin technician role holds `jobs.write` — and pinning either answer
    in a test would mint a product decision this file has no standing to make.
    PATCH is different: a tech editing the job in front of them is the
    documented intent (see create_job's "a job created in the field belongs to
    the tech who created it"), and it is the exact route GDXA-37 filed.

    A KNOWN HOLE, so the next person does not chase it: a PATCH that moves
    `lifecycle_stage` cannot be proven here either, and is not covered below.
    That one column is written by raw SQL — `UPDATE jobs SET lifecycle_stage =
    CAST(:ls AS job_lifecycle_stage) WHERE id = :jid` — which needs the PG
    enum cast, and on SQLite `id = :jid` compares a dashed uuid against the 32
    hex characters SQLite stores, matching nothing. The statement is a silent
    no-op here rather than a wrong write (SQLite resolves the unknown cast
    target to NUMERIC affinity, so a match would store '0'), so the two
    defects cancel and neither reaches production, where the write is correct.
    Half-fixing it — making the WHERE match without dialect-switching the
    CAST — would turn a no-op into a corrupt write. Left alone on purpose.
    """

    def test_the_assigned_technician_can_close_out(self, ctx):
        """The claim half's TRUE branch, and the live mobile path:
        MobileJobCloseoutDialog posts here from a tech's phone in a garage.
        If the fix broke this it would take field billing with it.

        This is the exact request that returned 201 for a STRANGER before the
        fix (Arm A above) — same route, same body, different caller."""
        client, db, job, _dep, be, _ = ctx
        be(ASSIGNED_USER, "technician")
        r = _call(client, "POST", f"/api/jobs/{job.id}/closeout",
                  {"parts": [], "hours": 2.5, "no_parts_used": True})
        assert r.status_code == 201, r.text[:400]
        assert db.execute(text("SELECT count(*) FROM job_closeouts")).scalar() == 1

    @pytest.mark.parametrize("role", ["owner", "admin", "dispatcher"])
    def test_the_office_tiers_can_close_out(self, ctx, role):
        """No office user has an assignment row, so they pass the claim half
        on jobs.read_all / WILDCARD. Dropping that half would lock the
        dispatcher's board out of its own buttons."""
        client, db, job, _dep, be, _ = ctx
        be(str(uuid4()), role)
        r = _call(client, "POST", f"/api/jobs/{job.id}/closeout",
                  {"parts": [], "hours": 2.5, "no_parts_used": True})
        assert r.status_code == 201, f"{role}: {r.text[:400]}"
        assert db.execute(text("SELECT count(*) FROM job_closeouts")).scalar() == 1

    def test_the_assigned_technician_can_start(self, ctx):
        """A second route, so the admission result is not one handler's
        accident."""
        client, db, job, _dep, be, _ = ctx
        be(ASSIGNED_USER, "technician")
        r = _call(client, "POST", f"/api/jobs/{job.id}/start", {})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assert _job_row(db, job.id)[0] == "in_progress"

    def test_the_office_tier_can_start(self, ctx):
        client, db, job, _dep, be, _ = ctx
        be(str(uuid4()), "dispatcher")
        r = _call(client, "POST", f"/api/jobs/{job.id}/start", {})
        assert r.status_code == 200, r.text[:300]
        db.expire_all()
        assert _job_row(db, job.id)[0] == "in_progress"

    def test_the_assigned_technician_can_patch_the_job(self, ctx):
        """GDXA-88. Byte-for-byte the request Arm A refuses with 404 — same
        route, same field, different caller — and the one this whole issue
        was filed about. Before the bind fix it answered 500 for EVERY caller,
        so the route's success path had never once run in this suite."""
        client, db, job, _dep, be, _ = ctx
        be(ASSIGNED_USER, "technician")
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"title": "renamed"})
        assert r.status_code == 200, r.text[:400]
        db.expire_all()
        assert _job_row(db, job.id)[1] == "renamed"

    @pytest.mark.parametrize("role", ["owner", "admin", "dispatcher"])
    def test_the_office_tiers_can_patch_the_job(self, ctx, role):
        """JobsView's edit dialog and the dispatch board both PATCH here, and
        no office user has an assignment row — they pass on the read_all /
        WILDCARD half."""
        client, db, job, _dep, be, _ = ctx
        be(str(uuid4()), role)
        r = _call(client, "PATCH", f"/api/jobs/{job.id}", {"title": f"by-{role}"})
        assert r.status_code == 200, f"{role}: {r.text[:400]}"
        db.expire_all()
        assert _job_row(db, job.id)[1] == f"by-{role}"

    def test_the_office_tier_can_soft_delete_the_job(self, ctx):
        """Soft delete, never hard (invariant #2): the row must survive with
        `deleted_at` set, or the audit and billing chains stop being
        reconstructable. Matched against both textual uuid forms for the same
        reason `_job_row` is."""
        client, db, job, _dep, be, _ = ctx
        be(str(uuid4()), "dispatcher")
        r = _call(client, "DELETE", f"/api/jobs/{job.id}", {})
        assert r.status_code == 200, r.text[:400]
        db.expire_all()
        raw = str(job.id)
        row = db.execute(
            text(
                "SELECT deleted_at FROM jobs WHERE CAST(id AS TEXT) IN (:j, :jh)"
            ),
            {"j": raw, "jh": raw.replace("-", "").lower()},
        ).first()
        assert row is not None, "the row was hard-deleted"
        assert row[0] is not None, "deleted_at was not set"

    def test_the_office_tier_can_uncomplete(self, ctx):
        """The fixture's job is completed, which is what /uncomplete requires;
        a 409 here would mean the handler never reached its state check."""
        client, db, job, _dep, be, _ = ctx
        be(str(uuid4()), "dispatcher")
        r = _call(client, "POST", f"/api/jobs/{job.id}/uncomplete",
                  {"reason": "wrong job closed"})
        assert r.status_code == 200, r.text[:400]
        db.expire_all()
        assert _job_row(db, job.id)[0] == "in_progress"

    def test_the_office_tier_can_close_without_work(self, ctx):
        """/close-without-work only accepts an open job, so open the fixture's
        completed job first, through the ORM."""
        client, db, job, _dep, be, _ = ctx
        job.lifecycle_stage = "scheduled"
        job.completed_at = None
        db.commit()
        be(str(uuid4()), "dispatcher")
        r = _call(client, "POST", f"/api/jobs/{job.id}/close-without-work",
                  {"reason": "duplicate of another job"})
        assert r.status_code == 200, r.text[:400]
        db.expire_all()
        row = _job_row(db, job.id)
        assert row[0] == "completed" and row[2] is not None

    def test_the_office_tier_can_reactivate(self, ctx):
        """/reactivate only accepts a cancelled job, so cancel it first —
        through the ORM, since the raw-SQL lifecycle write is the known hole
        described in this class's docstring."""
        client, db, job, _dep, be, _ = ctx
        job.lifecycle_stage = "cancelled"
        db.commit()
        be(str(uuid4()), "dispatcher")
        r = _call(client, "POST", f"/api/jobs/{job.id}/reactivate",
                  {"reason": "customer called back"})
        assert r.status_code == 200, r.text[:400]
        db.expire_all()
        assert _job_row(db, job.id)[0] != "cancelled"

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("follow-up", {}),
            ("spawn-return-visit", {"reason": "warranty callback"}),
        ],
    )
    def test_the_office_tier_can_spawn_a_child_job(self, ctx, path, body):
        """Both routes mint a second row off the first, and counting rows is
        not enough: a child minted with no `parent_job_id` is an orphan the
        original can never be reconciled against, and it would pass a count
        while being a silent-write defect wearing a 201. So assert the LINK.

        NOT asserted, deliberately: which KIND of child each route mints.
        Both set `is_return_visit=True` — `create_follow_up_job` does so
        explicitly (checked, not assumed) — so that column does not
        distinguish a follow-up from a warranty callback, and the reporting
        question the design note at the bottom of routers/jobs.py poses ("how
        many warranty visits did we do this month?") cannot be answered from
        it alone. Whether that is intended is jobs-dispatch's call about a
        metric, not something an authz test should pin in either direction.
        """
        client, db, job, _dep, be, _ = ctx
        be(str(uuid4()), "dispatcher")
        r = _call(client, "POST", f"/api/jobs/{job.id}/{path}", body)
        assert r.status_code == 201, f"{path}: {r.text[:400]}"
        raw = str(job.id)
        children = db.execute(
            text(
                "SELECT CAST(parent_job_id AS TEXT) FROM jobs "
                "WHERE CAST(id AS TEXT) NOT IN (:j, :jh)"
            ),
            {"j": raw, "jh": raw.replace("-", "").lower()},
        ).all()
        assert len(children) == 1, f"{path}: expected exactly one child, got {children}"
        parent = children[0][0]
        assert parent is not None, f"{path}: child was minted with no parent link"
        assert parent.replace("-", "").lower() == raw.replace("-", "").lower()

    def test_the_office_tier_can_reassign_the_job(self, ctx):
        """The dispatch board's own request, and the most common PATCH in the
        app: DispatchView drag-and-drop sends {assigned_tech_id, assigned_to,
        technician_id} (+ scheduled_at). It is a different branch of update_job
        from a title edit — it runs `_set_job_assignments`, the
        require-tech-for-scheduled-job gate and the appointment mirror — so a
        title-only admission test does not cover it."""
        client, db, job, _dep, be, cust = ctx
        _ = cust
        be(str(uuid4()), "dispatcher")
        r = _call(client, "PATCH", f"/api/jobs/{job.id}",
                  {"assigned_tech_ids": [STRANGER_TECH]})
        assert r.status_code == 200, r.text[:400]
        db.expire_all()
        raw = str(job.id)
        assigned = db.execute(
            text("SELECT assigned_to FROM jobs WHERE CAST(id AS TEXT) IN (:j, :jh)"),
            {"j": raw, "jh": raw.replace("-", "").lower()},
        ).scalar()
        assert assigned == STRANGER_TECH, f"assigned_to={assigned!r}"

    def test_a_status_change_lands_through_the_orm_half(self, ctx):
        """The JobsView dropdown and the board both PATCH a status, which is
        the branch that writes `jobs.status` from either the `status` or the
        `lifecycle_stage` key (update_job's `raw_status` fallback).

        Only the ORM half is asserted. The `lifecycle_stage` enum column is
        written by the raw SQL this class's docstring describes as a known
        hole — a no-op on SQLite — so asserting it here would pin the hole
        instead of the behaviour. `jobs.status` is the half a regression in
        this branch would take with it, and it does execute."""
        client, db, job, _dep, be, _ = ctx
        # The fixture's job is completed, and update_job now refuses to move a
        # finished job (409, job-stage-paths-plan §4.1) — so open it first,
        # through the ORM for the reason given above.
        job.lifecycle_stage = "scheduled"
        job.completed_at = None
        db.commit()
        be(ASSIGNED_USER, "technician")
        r = _call(client, "PATCH", f"/api/jobs/{job.id}",
                  {"lifecycle_stage": "in_progress"})
        assert r.status_code == 200, r.text[:400]
        db.expire_all()
        raw = str(job.id)
        status = db.execute(
            text("SELECT status FROM jobs WHERE CAST(id AS TEXT) IN (:j, :jh)"),
            {"j": raw, "jh": raw.replace("-", "").lower()},
        ).scalar()
        assert status is not None and status.lower().startswith("in"), (
            f"status={status!r} — the ORM half of the status write did not land"
        )


# Job-scoped write routes allowed to gate on a decorator permission ALONE,
# with no claim check — and why each one is not an instance of the class.
#
# The test below would be theatre without this list. Its first draft counted
# any `require_permission(` in the decorator as gated, which certified
# `delete_job` — gated on `jobs.write`, a key the builtin technician role
# holds — as safe. That is exactly the "green ratchet that cannot fail for
# your defect" CLAUDE.md warns about, written into the file whose whole
# argument is that `authz_sweep.py` is blind. The rule now: a decorator
# permission excuses a route only when the key is one NO field-tier role
# holds, so holding it IS the office-tier assertion the class demands.
OFFICE_ONLY_KEYS = {"invoices.write"}  # not in BUILTIN_ROLES["technician"]


class TestTheGateCoversTheWholeClass:
    """Structural companion to the arms above: if a new job-scoped write
    route is added to routers/jobs.py without a gate, the arms cannot catch
    it — they only drive the routes listed in ROUTES. This counts."""

    def test_the_office_only_keys_really_are_office_only(self):
        """Names the falsifier for the allowlist: if a field-tier role ever
        gains one of these keys, the route it excuses becomes an instance of
        the class and this test must go red before the excuse does harm."""
        from gdx_dispatch.core.permissions import BUILTIN_ROLES

        for key in OFFICE_ONLY_KEYS:
            assert key not in BUILTIN_ROLES["technician"], key

    def test_every_job_scoped_write_route_is_gated(self):
        import inspect
        import re

        from gdx_dispatch.routers import jobs as jobs_router

        src = inspect.getsource(jobs_router)
        # Split on the decorators so each chunk is one route + its handler.
        chunks = re.split(r"\n@router\.", src)
        ungated: list[str] = []
        for chunk in chunks[1:]:
            head, _, body = chunk.partition("\n")
            if not head.startswith(("post(", "patch(", "put(", "delete(")):
                continue
            if "{job_id}" not in head:
                continue
            handler = re.search(r"def (\w+)\(", body)
            name = handler.group(1) if handler else head[:60]
            handler_body = body.split("\n@router.")[0]
            gated = "job_write_denial(" in handler_body or any(
                f'require_permission("{k}")' in head for k in OFFICE_ONLY_KEYS
            )
            if not gated:
                ungated.append(name)
        assert ungated == [], (
            "job-scoped write routes in routers/jobs.py with no write gate: "
            f"{ungated}"
        )
