"""Regression tests for the silent-success sweep (fix/silent-success-class).

Each endpoint here used to report success while doing none (or only part) of
the promised work:

- POST /api/timeclock/submit-day answered "submitted" and recorded nothing.
- POST /api/payroll/entries was a raw INSERT omitting `created_at` that could
  never succeed on an ORM-built schema; DELETE answered {"ok": true} for rows
  it never touched (Uuid stored dashless on SQLite — raw `id = :dashed` can't
  match).
- POST /api/surveys/send wrote a `survey_sent` audit row while transmitting
  nothing, and the minted /survey/{token} URL fell through to the SPA shell.
- routers/admin_settings.py: PATCH settings/email faked ok + a false audit
  row; tax-jurisdiction PATCH/DELETE answered ok for unknown ids; unlock /
  google-unlink / tc-permissions used raw SQL that never matched on SQLite.
- POST /api/admin/users/invite returned a reset link whose token was stored
  nowhere, so every link 400ed as invalid.
- /api/estimate-nurture/* and the /api/admin-ops stubs fabricated success
  outright and are deleted.

Fixtures build their schema from the ORM (create_all), never hand-written
DDL, and every login dict carries BOTH "sub" and "user_id" (#701). Assertions
read the real rows back — a mock proves which arguments were passed, never
which value comes back.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import PayrollEntry, TimeclockEntry, User
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.test_surveys import _make_client as _make_survey_client

TENANT = "tenant-sss"


def _login(user_id: str, role: str) -> dict:
    # Both keys on purpose: the real login dict carries user_id, older tests
    # modelled only "sub" — issue #701 proved that shape wrong.
    return {"user_id": user_id, "sub": user_id, "tenant_id": TENANT, "role": role}


def _build_client(router, *, user: dict, module_keys: tuple[str, ...] = ()) -> TestClient:
    """One router, an ORM-built in-memory SQLite DB, tenant + login injected."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Base = TenantBase in tenant_models: one create_all covers every table,
    # audit_logs included.
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(router)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: dict(user)
    for key in module_keys:
        # require_module is lru_cached, so this is the SAME callable object
        # the router registered — the override actually lands.
        app.dependency_overrides[require_module(key)] = lambda: None

    client = TestClient(app, raise_server_exceptions=True)
    client.SessionLocal = SessionLocal  # type: ignore[attr-defined]
    client._engine = engine  # type: ignore[attr-defined]
    return client


def _teardown(client: TestClient) -> None:
    client.app.dependency_overrides.clear()
    client._engine.dispose()  # type: ignore[attr-defined]


def _audit_rows(SessionLocal, action: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(
            db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
        )


# ---------------------------------------------------------------------------
# 1. POST /api/timeclock/submit-day — the attestation is actually recorded
# ---------------------------------------------------------------------------

TECH_ID = "tech-user-1"


@pytest.fixture()
def timeclock_client():
    from gdx_dispatch.routers import timeclock as timeclock_mod

    client = _build_client(
        timeclock_mod.router,
        user=_login(TECH_ID, "technician"),
        module_keys=("timeclock",),
    )
    yield client
    _teardown(client)


def test_submit_day_returns_counts_and_writes_the_attestation(timeclock_client):
    target = datetime.now(UTC).date().isoformat()
    SessionLocal = timeclock_client.SessionLocal
    with SessionLocal() as db:
        for hour, minutes in ((8, 60), (13, 30)):
            start = f"{target}T{hour:02d}:00:00+00:00"
            db.add(TimeclockEntry(
                id=str(uuid4()), tenant_id=TENANT, technician_id=TECH_ID,
                clock_in_at=start, clock_out_at=f"{target}T{hour + 1:02d}:00:00+00:00",
                minutes=minutes, entry_type="clock", created_at=start, updated_at=start,
            ))
        db.commit()

    r = timeclock_client.post("/api/timeclock/submit-day", json={"date": target})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submitted"] is True
    assert body["date"] == target
    assert body["entries"] == 2
    assert body["total_minutes"] == 90

    rows = _audit_rows(SessionLocal, "timeclock_day_submitted")
    assert len(rows) == 1, "the attestation audit row is the whole point"
    row = rows[0]
    assert row.user_id == TECH_ID
    assert row.entity_type == "timeclock_day"
    assert row.entity_id == target
    assert row.details == {"date": target, "entries": 2, "total_minutes": 90}


# ---------------------------------------------------------------------------
# 2 + 3. Payroll entries — ORM insert that works, rowcount-gated soft delete
# ---------------------------------------------------------------------------

ADMIN_ID = str(uuid4())


@pytest.fixture()
def payroll_client():
    from gdx_dispatch.modules.payroll import router as payroll_mod

    client = _build_client(payroll_mod.router, user=_login(ADMIN_ID, "admin"))
    yield client
    _teardown(client)


def _create_payroll_entry(client: TestClient) -> str:
    r = client.post(
        "/api/payroll/entries",
        json={
            "tech_user_id": "tech-9",
            "period_start": "2026-09-01T00:00:00+00:00",
            "period_end": "2026-09-14T00:00:00+00:00",
            "hours_paid": "80.00",
            "gross_pay": "2400.00",
            "notes": "manual period entry",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_payroll_create_persists_a_row_with_created_at(payroll_client):
    entry_id = _create_payroll_entry(payroll_client)
    with payroll_client.SessionLocal() as db:
        row = db.get(PayrollEntry, UUID(entry_id))
        assert row is not None, "the old raw INSERT never produced a row"
        assert row.created_at is not None
        assert row.company_id == TENANT
        assert str(row.hours_paid) in {"80.00", "80.0", "80"}
    audits = _audit_rows(payroll_client.SessionLocal, "payroll_entry_created")
    assert [a.entity_id for a in audits] == [entry_id]
    assert audits[0].user_id == ADMIN_ID


def test_payroll_delete_soft_deletes_and_audits(payroll_client):
    entry_id = _create_payroll_entry(payroll_client)
    r = payroll_client.delete(f"/api/payroll/entries/{entry_id}")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    with payroll_client.SessionLocal() as db:
        row = db.get(PayrollEntry, UUID(entry_id))
        assert row.deleted_at is not None, "soft delete must actually land (SQLite dashless Uuid)"
    audits = _audit_rows(payroll_client.SessionLocal, "payroll_entry_deleted")
    assert [a.entity_id for a in audits] == [entry_id]


def test_payroll_delete_twice_is_404_not_fake_ok(payroll_client):
    entry_id = _create_payroll_entry(payroll_client)
    assert payroll_client.delete(f"/api/payroll/entries/{entry_id}").status_code == 200
    r = payroll_client.delete(f"/api/payroll/entries/{entry_id}")
    assert r.status_code == 404, "second delete used to answer {'ok': true}"


def test_payroll_delete_unknown_uuid_is_404(payroll_client):
    r = payroll_client.delete(f"/api/payroll/entries/{uuid4()}")
    assert r.status_code == 404


def test_payroll_delete_garbage_id_is_404(payroll_client):
    r = payroll_client.delete("/api/payroll/entries/not-a-uuid")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. Surveys — honest audit action, and a page that actually serves
# ---------------------------------------------------------------------------


@pytest.fixture()
def survey_client():
    tc = _make_survey_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _mint_send(client: TestClient, question: str = "How likely are you to recommend us?") -> dict:
    tpl = client.post(
        "/api/surveys/templates",
        json={"name": "NPS", "kind": "nps", "question": question},
    ).json()
    r = client.post("/api/surveys/send", json={"template_id": tpl["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def test_survey_send_audits_link_created_not_sent(survey_client):
    body = _mint_send(survey_client)
    assert body["public_url"] == f"/survey/{body['token']}"
    Session = survey_client._session  # type: ignore[attr-defined]
    assert _audit_rows(Session, "survey_sent") == [], (
        "the old action name recorded sends that never happened"
    )
    rows = _audit_rows(Session, "survey_link_created")
    assert len(rows) == 1
    assert rows[0].entity_type == "survey_send"


def test_public_survey_page_serves_html_with_the_question(survey_client):
    body = _mint_send(survey_client, question="How did we do on your door?")
    r = survey_client.get(body["public_url"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "How did we do on your door?" in r.text
    # The page's form posts to the public submit endpoint that already works.
    assert f"/api/surveys/public/{body['token']}" in r.text


def test_public_survey_page_form_target_records_a_response(survey_client):
    body = _mint_send(survey_client)
    r = survey_client.post(f"/api/surveys/public/{body['token']}", json={"score": 9})
    assert r.status_code == 201, r.text
    assert r.json()["score"] == 9


def test_public_survey_page_bogus_token_is_friendly_html_404(survey_client):
    r = survey_client.get("/survey/definitely-not-a-token")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html"), (
        "a customer on a phone gets a page, not JSON"
    )
    assert "no longer available" in r.text


def test_public_survey_page_after_response_is_html_404(survey_client):
    body = _mint_send(survey_client)
    assert survey_client.post(
        f"/api/surveys/public/{body['token']}", json={"score": 10}
    ).status_code == 201
    r = survey_client.get(body["public_url"])
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "no longer available" in r.text


def test_public_survey_page_expired_send_is_html_404(survey_client):
    from gdx_dispatch.models.tenant_models import SurveySend

    body = _mint_send(survey_client)
    Session = survey_client._session  # type: ignore[attr-defined]
    with Session() as s:
        row = s.execute(select(SurveySend)).scalar_one()
        row.expires_at = utcnow() - timedelta(days=1)
        s.commit()
    r = survey_client.get(body["public_url"])
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "no longer available" in r.text


# ---------------------------------------------------------------------------
# 5. routers/admin_settings.py — honesty fixes
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_client():
    from gdx_dispatch.routers import admin_settings as admin_settings_mod

    client = _build_client(
        admin_settings_mod.router,
        user=_login(str(uuid4()), "admin"),
        module_keys=("jobs",),
    )
    yield client
    _teardown(client)


def _seed_user(SessionLocal, **overrides) -> str:
    uid = uuid4()
    fields = dict(
        id=uid, username="seeded", email=f"{uid.hex[:8]}@example.com",
        role="technician", company_id=TENANT, active=True,
    )
    fields.update(overrides)
    with SessionLocal() as db:
        db.add(User(**fields))
        db.commit()
    return str(uid)


def _get_user(SessionLocal, user_id: str) -> User:
    with SessionLocal() as db:
        return db.get(User, UUID(user_id))


def test_admin_email_settings_patch_is_501_with_no_false_audit(settings_client):
    r = settings_client.patch("/api/admin/settings/email", json={"smtp_host": "smtp.example.com"})
    assert r.status_code == 501, "env-var backed config: there is nowhere to write it"
    assert _audit_rows(settings_client.SessionLocal, "email_settings_updated") == [], (
        "the old handler manufactured a false audit trail"
    )


def test_tax_jurisdiction_update_unknown_id_is_404(settings_client):
    r = settings_client.patch(
        f"/api/admin/tax-jurisdictions/{uuid4()}",
        json={"name": "Hennepin", "rate": 7.375},
    )
    assert r.status_code == 404, "unknown jid used to answer ok"


def test_tax_jurisdiction_create_update_delete_lifecycle(settings_client):
    created = settings_client.post(
        "/api/admin/tax-jurisdictions",
        json={"name": "Hennepin", "rate": 7.375, "is_default": True},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["ok"] is True and body["id"]
    jid = body["id"]

    updated = settings_client.patch(
        f"/api/admin/tax-jurisdictions/{jid}",
        json={"name": "Hennepin County", "rate": 7.525, "is_default": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json() == {"ok": True}

    deleted = settings_client.delete(f"/api/admin/tax-jurisdictions/{jid}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}

    again = settings_client.delete(f"/api/admin/tax-jurisdictions/{jid}")
    assert again.status_code == 404, "second delete used to answer ok"

    for action in ("tax_jurisdiction_created", "tax_jurisdiction_updated", "tax_jurisdiction_deleted"):
        assert [a.entity_id for a in _audit_rows(settings_client.SessionLocal, action)] == [jid]


def test_tax_jurisdiction_delete_unknown_id_is_404(settings_client):
    r = settings_client.delete(f"/api/admin/tax-jurisdictions/{uuid4()}")
    assert r.status_code == 404


def test_unlock_actually_resets_failed_login_count(settings_client):
    user_id = _seed_user(settings_client.SessionLocal, failed_login_count=3)
    r = settings_client.post(f"/api/admin/users/{user_id}/unlock")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    # The proof the ORM update matches on SQLite where raw dashed-uuid SQL
    # never could: the column really is 0 afterwards.
    assert _get_user(settings_client.SessionLocal, user_id).failed_login_count == 0
    assert [a.entity_id for a in _audit_rows(settings_client.SessionLocal, "user_unlocked")] == [user_id]


def test_unlock_unknown_user_is_404(settings_client):
    assert settings_client.post(f"/api/admin/users/{uuid4()}/unlock").status_code == 404


def test_unlock_garbage_id_is_404(settings_client):
    assert settings_client.post("/api/admin/users/not-a-uuid/unlock").status_code == 404


def test_google_unlink_clears_the_column(settings_client):
    user_id = _seed_user(settings_client.SessionLocal, google_id="google-oauth-123")
    r = settings_client.post(f"/api/admin/users/{user_id}/google-unlink")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert _get_user(settings_client.SessionLocal, user_id).google_id is None


def test_google_unlink_unknown_user_is_404(settings_client):
    assert settings_client.post(f"/api/admin/users/{uuid4()}/google-unlink").status_code == 404


def test_tc_permissions_sets_the_column(settings_client):
    user_id = _seed_user(settings_client.SessionLocal)
    r = settings_client.patch(
        f"/api/admin/users/{user_id}/tc-permissions", json={"tc_can_edit": True}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert _get_user(settings_client.SessionLocal, user_id).tc_can_edit is True


def test_tc_permissions_unknown_user_is_404(settings_client):
    r = settings_client.patch(
        f"/api/admin/users/{uuid4()}/tc-permissions", json={"tc_can_edit": True}
    )
    assert r.status_code == 404


def test_tc_permissions_empty_payload_changes_nothing(settings_client):
    user_id = _seed_user(settings_client.SessionLocal)
    r = settings_client.patch(f"/api/admin/users/{user_id}/tc-permissions", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "changed": 0}


# ---------------------------------------------------------------------------
# 6. POST /api/admin/users/invite — the reset token is actually stored
# ---------------------------------------------------------------------------


class _RecordingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.calls.append((key, ttl, value))


class _DownRedis:
    def setex(self, *args, **kwargs) -> None:
        raise RuntimeError("redis unavailable")


@pytest.fixture()
def ops_client():
    from gdx_dispatch.routers import admin_ops as admin_ops_mod

    admin_id = str(uuid4())
    client = _build_client(admin_ops_mod.router, user=_login(admin_id, "admin"))
    # require_permission + resolve_author_name read the users row.
    _seed_user(
        client.SessionLocal, id=UUID(admin_id), username="Pat Admin",
        email="pat.admin@example.com", role="admin",
    )
    yield client, admin_ops_mod
    _teardown(client)


def test_invite_stores_the_reset_token_in_redis(ops_client, monkeypatch):
    client, admin_ops_mod = ops_client
    fake = _RecordingRedis()
    monkeypatch.setattr(admin_ops_mod, "_auth_redis", fake)

    r = client.post(
        "/api/admin/users/invite",
        json={"email": "new.tech@example.com", "role": "technician"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    token = body["invite_token"]
    assert token, "with Redis up the link must be handed out"
    assert body["message"].endswith(f"/reset-password?token={token}")
    # The token landed where POST /reset-password actually looks — before the
    # fix it lived only in the response and every minted link 400ed.
    assert fake.calls == [
        (f"pw_reset:{token}", 7 * 24 * 3600, f"{body['id']}|{TENANT}")
    ]

    rows = _audit_rows(client.SessionLocal, "user_invited")
    assert len(rows) == 1
    details = rows[0].details
    assert details["invite_token_prefix"] == token[:8]
    assert details["reset_link_stored"] is True
    assert token not in json.dumps(details), (
        "the immutable audit log is no place for a live credential"
    )


def test_invite_with_redis_down_says_so_instead_of_a_dead_link(ops_client, monkeypatch):
    client, admin_ops_mod = ops_client
    monkeypatch.setattr(admin_ops_mod, "_auth_redis", _DownRedis())

    r = client.post(
        "/api/admin/users/invite",
        json={"email": "offline.invite@example.com", "role": "technician"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invite_token"] is None, "a link that cannot work must not be handed out"
    assert "Forgot Password" in body["message"]

    with client.SessionLocal() as db:
        row = db.execute(
            select(User).where(User.email == "offline.invite@example.com")
        ).scalar_one_or_none()
        assert row is not None, "the user row exists either way"

    rows = _audit_rows(client.SessionLocal, "user_invited")
    assert len(rows) == 1
    assert rows[0].details["reset_link_stored"] is False


# ---------------------------------------------------------------------------
# 7. Deleted fake-success routes stay deleted
# ---------------------------------------------------------------------------


def test_estimate_nurture_and_admin_ops_stub_routes_are_gone():
    from gdx_dispatch.app import create_app
    from gdx_dispatch.tests.conftest import iter_app_routes

    paths = {path for path, _route in iter_app_routes(create_app())}
    nurture = sorted(p for p in paths if p.startswith("/api/estimate-nurture"))
    assert nurture == [], f"estimate-nurture route is back: {nurture}"
    assert "/api/admin-ops" not in paths, "the empty-list stub is back"
    assert "/api/admin-ops/actions" not in paths, "the _ok_with_id() stub is back"
