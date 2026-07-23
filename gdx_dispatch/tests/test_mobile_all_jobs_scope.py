"""Company-wide mobile jobs scope (2026-07-22).

Doug: "can we have a company wide option and a spot in mobile for tech to
see all jobs." The option is the catalog setting
``tech_mobile.techs_see_all_jobs`` (default OFF); the spot is
``GET /api/mobile/jobs?scope=company`` + a My jobs / All jobs switch in
MobileJobsView.

Security contract pinned here (same shape as the creator-visibility rule
from the 2026-07-22 fix's /audit):

- scope=company is 403 for techs unless the tenant option is ON;
  dispatch/admin tier is always allowed.
- With the option ON a tech can LIST and READ any job — but the write
  gate is untouched: POST /start on someone else's job still 404s.
- The response's ``all_jobs_enabled`` is what the UI keys the toggle on,
  and it must track the same server-side rule.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers import mobile as mobile_router

TECH_A = {"user_id": "user-a", "role": "technician", "tenant_id": "tenant-a"}
TECH_B = {"user_id": "user-b", "role": "technician", "tenant_id": "tenant-a"}
ADMIN = {"user_id": "user-admin", "role": "admin", "tenant_id": "tenant-a"}


def _as_json(response) -> dict:
    return json.loads(response.body)


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    return req


@pytest.fixture()
def session_factory(tmp_path):
    db_file = tmp_path / "all_jobs_scope_test.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def _seed(SessionLocal) -> dict[str, str]:
    """Two techs; one job assigned to tech B. Tech A has no claim on it."""
    db = SessionLocal()
    now = datetime.now(UTC)
    job_id = f"job-{uuid4().hex[:8]}"
    try:
        for tech_id, user_id, name in (
            ("tech-a", "user-a", "Alice Tech"),
            ("tech-b", "user-b", "Bob Tech"),
        ):
            db.execute(
                text(
                    "INSERT INTO technicians (id, company_id, user_id, name, active, created_at) "
                    "VALUES (:id, 'tenant-a', :uid, :name, 1, :now)"
                ),
                {"id": tech_id, "uid": user_id, "name": name, "now": now},
            )
        db.execute(
            text(
                "INSERT INTO jobs (id, company_id, title, dispatch_status, "
                "created_by, assigned_to, scheduled_at, created_at, deleted_at, "
                "signature_data, signed_by) "
                "VALUES (:id, 'tenant-a', 'Bobs job', 'assigned', "
                "'user-b', 'tech-b', NULL, :now, NULL, "
                "'data:image/png;base64,SIGBLOB', 'Jane Customer')"
            ),
            {"id": job_id, "now": now},
        )
        db.commit()
        return {"job_id": job_id}
    finally:
        db.close()


def _enable_setting(SessionLocal) -> None:
    db = SessionLocal()
    try:
        db.add(AppSettings(tenant_mobile_settings={"tech_mobile.techs_see_all_jobs": True}))
        db.commit()
    finally:
        db.close()


def _list(SessionLocal, user, scope="mine"):
    db = SessionLocal()
    try:
        return mobile_router.mobile_all_my_jobs(
            request=_request(), scope=scope, current_user=user, db=db
        )
    finally:
        db.close()


def test_company_scope_403_when_setting_off(session_factory):
    _seed(session_factory)
    r = _list(session_factory, TECH_A, scope="company")
    assert r.status_code == 403
    # And the flag the UI keys the toggle on says OFF.
    mine = _list(session_factory, TECH_A, scope="mine")
    assert _as_json(mine)["all_jobs_enabled"] is False


def test_company_scope_returns_other_techs_jobs_when_enabled(session_factory):
    seed = _seed(session_factory)
    _enable_setting(session_factory)

    mine = _list(session_factory, TECH_A, scope="mine")
    assert seed["job_id"] not in [j["id"] for j in _as_json(mine)["jobs"]]
    assert _as_json(mine)["all_jobs_enabled"] is True

    company = _list(session_factory, TECH_A, scope="company")
    assert company.status_code == 200
    data = _as_json(company)
    assert data["scope"] == "company"
    ids = {j["id"]: j for j in data["jobs"]}
    assert seed["job_id"] in ids
    # Whose job it is travels with the row for the company-wide UI.
    assert ids[seed["job_id"]]["assigned_tech_name"] == "Bob Tech"


def test_company_scope_always_allowed_for_dispatch_tier(session_factory):
    seed = _seed(session_factory)  # setting stays OFF
    r = _list(session_factory, ADMIN, scope="company")
    assert r.status_code == 200
    assert seed["job_id"] in [j["id"] for j in _as_json(r)["jobs"]]


def test_invalid_scope_is_400(session_factory):
    _seed(session_factory)
    r = _list(session_factory, TECH_A, scope="everything")
    assert r.status_code == 400


def test_setting_grants_detail_read_but_never_write(session_factory):
    """THE contract: company-wide visibility is read-only. With the option
    ON, tech A can open tech B's job detail — but /start still 404s."""
    seed = _seed(session_factory)
    _enable_setting(session_factory)

    db = session_factory()
    try:
        detail = mobile_router.get_mobile_job_detail(
            job_id=seed["job_id"], request=_request(), current_user=TECH_A, db=db
        )
        assert detail.status_code == 200

        start = mobile_router.mobile_job_start(
            job_id=seed["job_id"], request=_request(), current_user=TECH_A, db=db
        )
        assert start.status_code == 404, (
            "techs_see_all_jobs must NOT grant write access — "
            f"got {start.status_code} from /start"
        )
    finally:
        db.close()


def test_setting_never_satisfies_the_shared_write_gate(session_factory):
    """All ~18 mutating mobile endpoints route through _assert_job_access —
    pin the gate itself: with the option ON, a non-assigned tech still gets
    404 from it, so no write endpoint can be opened by this setting."""
    seed = _seed(session_factory)
    _enable_setting(session_factory)
    db = session_factory()
    try:
        with pytest.raises(HTTPException) as exc:
            mobile_router._assert_job_access(db, _request(), TECH_A, seed["job_id"])
        assert exc.value.status_code == 404
    finally:
        db.close()


def test_company_grant_redacts_signature_blob(session_factory):
    """/audit 2026-07-22: company-wide browsing must not ship the raw
    customer signature. The assigned tech still receives it; the
    company-grant viewer gets None (signed_by metadata may remain)."""
    seed = _seed(session_factory)
    _enable_setting(session_factory)
    db = session_factory()
    try:
        company_view = mobile_router.get_mobile_job_detail(
            job_id=seed["job_id"], request=_request(), current_user=TECH_A, db=db
        )
        assert _as_json(company_view)["job"]["signature_data"] is None

        assigned_view = mobile_router.get_mobile_job_detail(
            job_id=seed["job_id"], request=_request(), current_user=TECH_B, db=db
        )
        assert _as_json(assigned_view)["job"]["signature_data"] == (
            "data:image/png;base64,SIGBLOB"
        )
    finally:
        db.close()


def test_setting_off_keeps_detail_closed(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        with pytest.raises(HTTPException) as exc:
            mobile_router.get_mobile_job_detail(
                job_id=seed["job_id"], request=_request(), current_user=TECH_A, db=db
            )
        assert exc.value.status_code == 404
    finally:
        db.close()
