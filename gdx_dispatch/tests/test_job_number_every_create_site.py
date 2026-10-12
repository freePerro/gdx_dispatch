"""Every job is born with a job_number — GDXA-833.

Ledger [3] (2026-10-03): five of eight job-creation sites never assigned
``job_number``; prod carried four open service calls with no number. Office
staff find a job by its number, and an invoice quotes it, so an unnumbered
job is one nobody can refer to.

Two layers, because each catches what the other cannot:

* The endpoint tests drive the real routes (service call intake, the service
  trigger run, the follow-up job) and read the stored row back. The
  allocator is replaced: it locks ``tenant_settings`` ``FOR UPDATE``, which
  SQLite cannot parse, and its own monotonicity is pinned elsewhere
  (test_job_number_uniqueness.py). What these tests prove is the router's
  half: it calls the allocator, commits the counter, and puts the number it
  got on the row it creates. A numbering failure must leave the job created
  and unnumbered, never unmade (the contract create_job set).

* The structural guard walks every ``Job(`` construction outside tests and
  migrations and fails on one that does not pass ``job_number``. It is the
  class guard: a sixth creation site added tomorrow goes red here. The
  allowlist holds the sites other agents own and have not fixed yet, and it
  goes red when an entry is stale, so a fix there must delete its line.
"""
from __future__ import annotations

import ast
import pathlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Job, ServiceTrigger
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "11111111-2222-3333-4444-555555555555"
ROOT = pathlib.Path(__file__).resolve().parents[1]


class _FakeControlSession:
    """Stands in for ``SessionLocal()``: records whether the counter was committed."""

    def __init__(self, log: list) -> None:
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self) -> None:
        self._log.append("commit")


@pytest.fixture
def numbering(monkeypatch):
    """Patch the allocator in each router that calls it; returns the call log."""
    calls: list = []
    state = {"seq": 0, "fail": False}

    def fake_next_job_number(cdb, tenant_id, *, customer_name=None):
        if state["fail"]:
            raise RuntimeError("numbering down")
        state["seq"] += 1
        calls.append(("alloc", str(tenant_id), customer_name))
        return f"JOB-T-{state['seq']:03d}"

    def fake_session_local():
        return _FakeControlSession(calls)

    for mod in (
        "gdx_dispatch.routers.service_calls",
        "gdx_dispatch.routers.service_triggers",
        "gdx_dispatch.routers.jobs",
    ):
        monkeypatch.setattr(f"{mod}.next_job_number", fake_next_job_number)
        monkeypatch.setattr(f"{mod}.SessionLocal", fake_session_local)
    return calls, state


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setattr("gdx_dispatch.routers.service_calls.company_id", lambda: TENANT)
    engine = make_fresh_db()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES ('g-833', :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"tid": TENANT},
    )
    db.commit()

    from gdx_dispatch.core.auth import get_current_user as core_get_current_user
    from gdx_dispatch.routers.auth import get_current_user as routers_get_current_user
    from gdx_dispatch.routers.jobs import router as jobs_router
    from gdx_dispatch.routers.service_calls import router as calls_router
    from gdx_dispatch.routers.service_triggers import router as triggers_router

    caller = {"user_id": str(uuid4()), "sub": None, "tenant_id": TENANT, "role": "dispatcher"}
    caller["sub"] = caller["user_id"]
    app = FastAPI()
    for r in (jobs_router, calls_router, triggers_router):
        app.include_router(r)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[core_get_current_user] = lambda: caller
    app.dependency_overrides[routers_get_current_user] = lambda: caller

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "t833"}
        request.state.tenant_id = TENANT
        request.state.user = caller
        return await call_next(request)

    cust = Customer(id=uuid4(), name="Acme Doors", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    db.commit()
    yield TestClient(app, raise_server_exceptions=False), db, cust
    db.close()
    engine.dispose()


def _job_numbers(db) -> dict[str, str | None]:
    """id (dashless, lower) -> job_number, read raw so the ORM cannot help."""
    rows = db.execute(text("SELECT CAST(id AS TEXT), job_number FROM jobs")).all()
    return {str(i).replace("-", "").lower(): n for i, n in rows}


def _key(job_id: str) -> str:
    return job_id.replace("-", "").lower()


# ---------------------------------------------------------------------------
# Service call intake — routers/service_calls.py
# ---------------------------------------------------------------------------
def test_service_call_is_born_numbered(ctx, numbering):
    client, db, cust = ctx
    calls, _ = numbering
    r = client.post("/api/service-calls", json={
        "customer_id": str(cust.id), "customer_name": "acme (typed)",
        "problem_description": "Spring broken", "urgency": "urgent",
    })
    assert r.status_code == 201, r.text[:400]
    body = r.json()
    assert body["job_number"] == "JOB-T-001"
    db.expire_all()
    assert _job_numbers(db)[_key(body["job_id"])] == "JOB-T-001"
    assert calls == [("alloc", TENANT, "Acme Doors"), "commit"]


def test_service_call_survives_a_numbering_failure(ctx, numbering):
    client, db, _cust = ctx
    _, state = numbering
    state["fail"] = True
    r = client.post("/api/service-calls", json={
        "customer_name": "Walk-in", "problem_description": "Opener dead",
    })
    assert r.status_code == 201, r.text[:400]
    db.expire_all()
    assert _job_numbers(db)[_key(r.json()["job_id"])] is None


def test_service_call_bad_customer_id_burns_no_number(ctx, numbering):
    """The counter commits and cannot roll back, so the one parse that can
    refuse the request runs before it."""
    client, _db, _cust = ctx
    calls, _ = numbering
    r = client.post("/api/service-calls", json={
        "customer_id": "not-a-uuid", "customer_name": "X",
        "problem_description": "Opener dead",
    })
    assert r.status_code == 500
    assert calls == []


# ---------------------------------------------------------------------------
# Service trigger run — routers/service_triggers.py
# ---------------------------------------------------------------------------
def _add_trigger(db, cust, *, auto: bool = True, customer_id: str | None = None):
    now = datetime.now(UTC)
    trigger = ServiceTrigger(
        id=uuid4(), company_id=TENANT, agreement_id=str(uuid4()),
        customer_id=customer_id or str(cust.id), next_due=now - timedelta(days=1),
        interval_months=12, auto_create_job=auto, status="active",
        created_at=now, updated_at=now,
    )
    db.add(trigger)
    db.commit()
    return trigger


def test_trigger_run_numbers_every_job_it_mints(ctx, numbering):
    client, db, cust = ctx
    calls, _ = numbering
    _add_trigger(db, cust)
    _add_trigger(db, cust)
    r = client.post("/api/service-triggers/run")
    assert r.status_code == 200, r.text[:400]
    created = r.json()["jobs_created"]
    assert len(created) == 2
    db.expire_all()
    stored = _job_numbers(db)
    got = sorted(stored[_key(c["job_id"])] for c in created)
    assert got == ["JOB-T-001", "JOB-T-002"]
    assert sorted(c["job_number"] for c in created) == got
    # The customer's name reached the allocator (templates may use it).
    assert [c for c in calls if c != "commit"] == [("alloc", TENANT, "Acme Doors")] * 2
    assert calls.count("commit") == 2


def test_one_bad_trigger_neither_fails_the_run_nor_spends_a_number(ctx, numbering):
    """/audit 2026-10-12: a number cannot be handed back and the run's jobs
    commit together, so a trigger whose customer cannot be resolved (the
    create endpoint takes any string) must be refused before a number is
    taken. Before this, it 500ed the run after the good triggers had spent
    theirs — on every retry."""
    client, db, cust = ctx
    calls, _ = numbering
    _add_trigger(db, cust)
    bad_text = _add_trigger(db, cust, customer_id="legacy-cust-7")
    bad_missing = _add_trigger(db, cust, customer_id=str(uuid4()))
    _add_trigger(db, cust)
    due_before = {str(t.id): t.next_due for t in (bad_text, bad_missing)}
    r = client.post("/api/service-triggers/run")
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert len(body["jobs_created"]) == 2
    assert sorted(s["trigger_id"] for s in body["triggers_skipped"]) == sorted(due_before)
    # Exactly the two good triggers took a number.
    assert calls.count("commit") == 2
    db.expire_all()
    numbered = [n for n in _job_numbers(db).values() if n]
    assert sorted(numbered) == ["JOB-T-001", "JOB-T-002"]
    # A skipped trigger is left due, so the next run still shows it.
    for t in (bad_text, bad_missing):
        db.refresh(t)
        assert t.next_due == due_before[str(t.id)]
        assert t.last_triggered is None


def test_trigger_run_survives_a_numbering_failure(ctx, numbering):
    client, db, cust = ctx
    _, state = numbering
    state["fail"] = True
    _add_trigger(db, cust)
    r = client.post("/api/service-triggers/run")
    assert r.status_code == 200, r.text[:400]
    created = r.json()["jobs_created"]
    assert len(created) == 1
    db.expire_all()
    assert _job_numbers(db)[_key(created[0]["job_id"])] is None


def test_trigger_without_auto_create_burns_no_number(ctx, numbering):
    client, _db, cust = ctx
    calls, _ = numbering
    _add_trigger(_db, cust, auto=False)
    r = client.post("/api/service-triggers/run")
    assert r.status_code == 200, r.text[:400]
    assert r.json()["jobs_created"] == []
    assert calls == []


# ---------------------------------------------------------------------------
# Follow-up job — routers/jobs.py create_follow_up_job
# ---------------------------------------------------------------------------
def test_follow_up_job_is_born_numbered(ctx, numbering):
    client, db, cust = ctx
    calls, _ = numbering
    parent = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Fix",
                 description="", job_number="JOB-PARENT", lifecycle_stage="completed")
    db.add(parent)
    db.commit()
    r = client.post(f"/api/jobs/{parent.id}/follow-up", json={})
    assert r.status_code == 201, r.text[:400]
    body = r.json()
    assert body["job_number"] == "JOB-T-001"
    db.expire_all()
    stored = _job_numbers(db)
    assert stored[_key(body["id"])] == "JOB-T-001"
    assert stored[_key(str(parent.id))] == "JOB-PARENT"
    assert calls == [("alloc", TENANT, "Acme Doors"), "commit"]


def test_follow_up_job_survives_a_numbering_failure(ctx, numbering):
    client, db, cust = ctx
    _, state = numbering
    state["fail"] = True
    parent = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Fix",
                 description="", lifecycle_stage="completed")
    db.add(parent)
    db.commit()
    r = client.post(f"/api/jobs/{parent.id}/follow-up", json={})
    assert r.status_code == 201, r.text[:400]
    db.expire_all()
    assert _job_numbers(db)[_key(r.json()["id"])] is None


# ---------------------------------------------------------------------------
# The class guard
# ---------------------------------------------------------------------------
# path (relative to gdx_dispatch/) -> count of Job(...) calls without
# job_number that are known and owned elsewhere. Delete the line when you fix
# the site; a stale count is red.
UNNUMBERED_ALLOWLIST: dict[str, int] = {
    # estimates-pricing: estimate -> job conversion (sibling of GDXA-833).
    "routers/estimates.py": 1,
    # platform-core: onboarding sample job (sibling of GDXA-833).
    "routers/onboarding.py": 1,
    # Demo seed data, not a user-facing create path.
    "docker/demo/seed_demo.py": 3,
}


def _job_constructions() -> tuple[Counter, Counter]:
    """(every Job(...) call, the ones without job_number), per file."""
    seen: Counter = Counter()
    found: Counter = Counter()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "migrations/")) or "/node_modules/" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name != "Job":
                continue
            seen[rel] += 1
            if not any(k.arg == "job_number" for k in node.keywords):
                found[rel] += 1
    return seen, found


def test_every_job_construction_passes_a_job_number():
    _seen, found = _job_constructions()
    new = {p: n for p, n in found.items() if n > UNNUMBERED_ALLOWLIST.get(p, 0)}
    assert not new, (
        "Job(...) constructed without job_number — a job born unnumbered "
        "(GDXA-833). Allocate one as routers/jobs.py create_job does "
        f"(next_job_number in its own SessionLocal): {new}"
    )
    stale = {p: (n, found.get(p, 0)) for p, n in UNNUMBERED_ALLOWLIST.items()
             if found.get(p, 0) < n}
    assert not stale, (
        "UNNUMBERED_ALLOWLIST is stale (allowed, found) — a site was fixed; "
        f"lower or delete its entry: {stale}"
    )


def test_the_guard_sees_the_create_sites():
    """A guard that cannot go red proves nothing. If the walk missed the
    routers (wrong root, a parse failure swallowed) it would find no
    unnumbered site and pass; so pin that it sees the numbered ones too."""
    seen, found = _job_constructions()
    for rel in ("routers/jobs.py", "routers/service_calls.py",
                "routers/service_triggers.py", "api/public_router.py"):
        assert seen[rel] >= 1, f"scan did not see the Job(...) in {rel}"
        assert found[rel] == 0, f"{rel} has an unnumbered Job(...)"
