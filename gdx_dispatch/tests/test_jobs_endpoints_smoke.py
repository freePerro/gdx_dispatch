"""Always-on smoke for the core job-lifecycle write endpoints.

WHY THIS EXISTS — 2026-05-19 incident: ``POST /api/jobs`` 500'd on prod
for 6 days (JobCreate missing ``holding_area_id`` that ``create_job``
read). It wasn't caught because **no test in the normal suite ever
executes ``create_job``**: every real ``POST /api/jobs`` test lives in
``gdx_dispatch/tests/e2e/`` (hits a live VPS, ``-m e2e`` → excluded by
``pytest.ini`` ``addopts``); ``test_35_e2e_workflow_chain`` is skipped in
unit runs; ``test_01_gdx_scaffold`` skips without docker-postgres;
``test_response_comparison`` mocks the endpoint.

This test runs in the DEFAULT suite (no e2e marker, no docker, in-memory
SQLite) and drives ``POST /api/jobs`` through a real ``TestClient`` with
``raise_server_exceptions=True`` — an unhandled exception in the handler
(exactly the original AttributeError shape) raises and fails the test;
a returned 5xx is caught by the explicit ``< 500`` assertion. Harness
mirrors the established ``test_service_agreements._make_client`` pattern.

Scope is deliberate and honest: SQLite can faithfully execute the
``create_job`` path (its Postgres-only job-number step is swallowed by
the handler's own try/except, so a genuine code/schema bug — like the
original missing pydantic field — still hard-fails this test; proven).
``create_job`` + the holding_area 400 are the faithful, always-on guard
for the actual incident class. ``GET`` and ``PATCH /api/jobs/{id}`` joined
them in GDXA-88, once the raw-str-to-``Uuid`` bind that made them
unexecutable on SQLite was fixed. The closeout endpoint still uses
Postgres-only SQL that SQLite cannot run faithfully — see the note below
the tests. No skip/xfail: every test here actually executes the code it
asserts on.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import make_fresh_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.jobs import router as jobs_router

# A real UUID: next_job_number / tenant-gate paths parse tenant_id as a
# UUID; a non-UUID just gets swallowed by create_job's try/except (still
# a valid <500 path, but it spams an ERROR traceback into test output).
TENANT_ID = "00000000-0000-4000-8000-0000000000aa"


@pytest.fixture
def client() -> TestClient:
    engine = make_fresh_db()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.execute(
        text(
            "INSERT OR IGNORE INTO company_module_grants "
            "(id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
        ),
        {"id": f"grant-{TENANT_ID}", "tid": TENANT_ID},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT_ID}
        request.state.request_id = "smoke"
        return await call_next(request)

    app.include_router(jobs_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-smoke",
        "sub": "user-smoke",
        "role": "admin",
        "tenant_id": TENANT_ID,
    }
    # raise_server_exceptions=True: an unhandled exception in a handler
    # (the original bug's shape) propagates and fails the test loudly.
    return TestClient(app, raise_server_exceptions=True)


def test_post_jobs_valid_payload_is_not_5xx(client: TestClient) -> None:
    """The exact regression: a minimal valid create must not 500.
    Pre-fix this raised AttributeError('holding_area_id')."""
    r = client.post("/api/jobs", json={"title": f"smoke {uuid4().hex[:8]}"})
    assert r.status_code < 500, f"POST /api/jobs 5xx'd: {r.status_code} {r.text[:400]}"
    assert r.status_code in (200, 201), f"unexpected status {r.status_code}: {r.text[:300]}"


def test_post_jobs_bogus_holding_area_id_is_400_not_500(client: TestClient) -> None:
    """The auditor-mandated validation: unknown holding_area_id is a
    clean 400, never a 500 or a silent phantom-lane write."""
    r = client.post(
        "/api/jobs",
        json={"title": "smoke bad-ha", "holding_area_id": "does-not-exist"},
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:300]}"
    assert "holding_area_id" in r.text


def test_get_and_patch_one_job_are_not_5xx(client: TestClient) -> None:
    """GDXA-88: the round trip POST -> GET {id} -> PATCH {id}.

    Both single-job routes validated `job_id` as a uuid and then bound the raw
    path STRING to `Job.id`, a `Uuid()` column. Postgres takes either form;
    SQLite raises StatementError ('str' object has no attribute 'hex'), which
    each handler's own `except SQLAlchemyError` returned as a 500 — the exact
    complaint GDXA-37 filed. `get_job` is the one route of the seven with no
    other always-on guard: its only coverage was `tests/e2e/`, which
    `pytest.ini` excludes, so reverting its bind alone turned nothing red.

    Deliberately a <500 smoke, matching this file's premise, not an authz
    test — the caller here is an admin. The object-level gate on PATCH is
    covered by tests/test_jobs_write_authz.py.
    """
    created = client.post("/api/jobs", json={"title": f"smoke {uuid4().hex[:8]}"})
    assert created.status_code < 500, f"POST setup 5xx'd: {created.text[:300]}"
    job_id = created.json().get("id")
    assert job_id, f"POST /api/jobs returned no id: {created.text[:300]}"

    got = client.get(f"/api/jobs/{job_id}")
    assert got.status_code == 200, f"GET /api/jobs/{{id}}: {got.status_code} {got.text[:300]}"
    assert str(got.json().get("id")).replace("-", "") == str(job_id).replace("-", "")

    patched = client.patch(f"/api/jobs/{job_id}", json={"title": "smoke renamed"})
    assert patched.status_code == 200, (
        f"PATCH /api/jobs/{{id}}: {patched.status_code} {patched.text[:300]}"
    )
    assert client.get(f"/api/jobs/{job_id}").json().get("title") == "smoke renamed"


# NOTE on POST /api/jobs/{id}/closeout — deliberately NOT driven
# end-to-end here.
#
# PATCH /api/jobs/{id} WAS in this note until GDXA-88, on the strength of
# the third reason below; that reason is now gone (it bound a raw str to a
# `Uuid` column, which was a portability defect rather than a property of
# SQLite) and the round trip is driven above. The closeout handler's
# remaining Postgres-only SQL (`SELECT ... FOR UPDATE` in next_job_number;
# a control-plane `tenant_settings` workflow-gate read) is genuinely not
# something SQLite can execute faithfully.
# Forcing it through the in-memory harness produces failures caused by
# the dialect, not by the code — a flaky test that blocks CI for the
# wrong reason. Reintroducing it as skip/xfail would recreate the exact
# blind spot this file exists to close (a green suite that doesn't run
# the code).
#
# The lesson worth keeping from GDXA-88: "SQLite cannot run this" and "this
# code is not portable" look identical from here, and only the second one is
# ours to fix. Check which it is before writing the route off.
#
# The ATTRIBUTE shape of the original bug class (a handler reading a
# `payload.<attr>` the request schema doesn't declare — the exact 6-day
# 500) IS covered, DB-free, for every router incl. closeout_job by
# test_router_payload_attr_contract.py. Honest limit: that scan sees
# `payload.<attr>` only. `update_job` reads via
# `data = payload.model_dump(); data["holding_area_id"]` — the SAME bug
# class in dict-key shape, which an attribute scan cannot see.
#
# As of GDXA-88 the always-on suite DOES execute that dict-key path: the
# round trip above plus the PATCH admission tests in
# test_jobs_write_authz.py drive update_job for title, status /
# lifecycle_stage and assignment. The residual is narrower than it was and
# still worth stating — a dict-key read reached only by a field no test
# sends (`holding_area_id` among them) is still unexecuted here, and only
# the `-m e2e` suite covers it. Closing it (a model_dump/dict-key scan) is
# a tracked follow-up.
