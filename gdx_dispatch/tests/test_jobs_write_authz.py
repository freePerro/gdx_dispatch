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

    Driven on /closeout and /start. NOT on /uncomplete, /reactivate,
    /follow-up or /spawn-return-visit: those four bind `Job.id == job_id`
    with a raw str, which the SQLite harness rejects ('str' object has no
    attribute 'hex') long before any gate is involved. Pre-existing, unrelated
    to this fix, and it means an admitted caller on those four cannot be
    proven here at all — see this file's companion note in the PR body. Their
    refusal arms above still execute, because the gate answers before the
    broken query runs.
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
