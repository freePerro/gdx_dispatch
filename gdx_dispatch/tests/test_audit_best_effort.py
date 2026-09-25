"""GDXA-44/GDXA-48: a failed audit write must not poison the caller's session.

``log_audit_event_sync`` ends in ``db.flush()``. When the storage layer refuses
that INSERT, SQLAlchemy deactivates the whole transaction: every later
``execute`` or ``commit`` on that session raises ``PendingRollbackError``. Six
hand-rolled ``_audit`` helpers caught the first exception and logged it, then
handed the poisoned session straight back to the handler.

Two outcomes on prod, both bad:

- the handler touches the session again and the request 500s — while the
  mutation it is reporting as failed has *already committed* (the audit is the
  last step). ``custom_fields.create_definition`` is that shape.
- the handler returns a plain literal and answers 200 — the change persists and
  the trail row never exists. ``admin_settings`` is that shape.

Nothing anywhere asserted either behaviour before this file
(``grep -rn "audit_failed" gdx_dispatch/tests/`` was empty).

**The failure is made at the storage layer, never in Python.** Every test here
installs a real ``BEFORE INSERT ON audit_logs ... RAISE(ABORT)`` trigger. A
monkeypatched audit function would raise before ``flush()`` and would therefore
never reproduce the thing that actually hurts — a *deactivated transaction*.

``_AUDIT_GUARD_INITIALIZED`` is keyed on the engine, so the fresh engine each
test builds also exercises ``ensure_audit_table``'s first-use commit, which is
the hard path: that commit has to happen outside the savepoint or it releases
the savepoint that is supposed to contain the failure.
"""
from __future__ import annotations

import contextlib
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request as StarletteRequest

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import TaxJurisdiction, User
from gdx_dispatch.routers.auth import get_current_user

TENANT = "11111111-1111-1111-1111-111111111448"


def _login(role: str = "admin") -> dict:
    uid = str(uuid4())
    # Both keys on purpose — the real login dict carries user_id (#701).
    return {"user_id": uid, "sub": uid, "tenant_id": TENANT, "role": role}


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return engine


def _refuse_audit_inserts(engine) -> None:
    """Make audit_logs refuse every INSERT, from the database itself.

    This is the mechanism GDXA-44's triage used. It matters that the refusal
    comes from the storage layer: the defect is what a failed *flush* does to
    the session, and only a real statement failure produces that.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TRIGGER audit_logs_refuse_insert
                BEFORE INSERT ON audit_logs
                BEGIN
                    SELECT RAISE(ABORT, 'audit storage refuses this row');
                END;
                """
            )
        )


def _request(user: dict) -> Request:
    """A real Starlette Request, not a stub — the audit module reads
    ``state.tenant``, ``state.user``, ``headers`` and ``client`` off it."""
    req = StarletteRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-request-id", b"req-gdxa-48")],
            "client": ("203.0.113.9", 4321),
            "query_string": b"",
        }
    )
    req.state.tenant = {"id": TENANT}
    req.state.user = dict(user)
    return req


def _session_is_usable(db) -> bool:
    """Can the caller keep using the session it handed to the audit helper?"""
    try:
        db.execute(select(1))
        db.commit()
    except PendingRollbackError:
        return False
    return True


def _build_client(router, *, user: dict, module_keys: tuple[str, ...] = ()) -> TestClient:
    """One router, an ORM-built SQLite DB, tenant + login injected.

    ``get_db`` yields a session per request and closes it WITHOUT committing,
    exactly like ``core.database.get_db`` — a row that is only flushed never
    lands (#700).
    """
    engine = _engine()
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
        # require_module is lru_cached — this is the same callable the router
        # registered, so the override actually lands.
        app.dependency_overrides[require_module(key)] = lambda: None

    client = TestClient(app, raise_server_exceptions=False)
    client.SessionLocal = SessionLocal  # type: ignore[attr-defined]
    client._engine = engine  # type: ignore[attr-defined]
    return client


def _rows(SessionLocal, model, **where):
    with SessionLocal() as db:
        stmt = select(model)
        for col, val in where.items():
            stmt = stmt.where(getattr(model, col) == val)
        return list(db.execute(stmt).scalars().all())


# ── the shared helper ────────────────────────────────────────────────────────


def test_helper_records_the_row_and_reports_true():
    """Control: nothing is refusing, so the row lands and is durable."""
    from gdx_dispatch.core.audit import audit_best_effort

    engine = _engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    user = _login()
    try:
        with SessionLocal() as db:
            assert audit_best_effort(
                db,
                action="thing_happened",
                entity_type="thing",
                entity_id="thing-1",
                tenant_id=TENANT,
                user_id=user["user_id"],
                request=_request(user),
                details={"k": "v"},
            ) is True

        rows = _rows(SessionLocal, AuditLog, action="thing_happened")
        assert len(rows) == 1, "the audit row must be committed, not merely flushed"
        assert rows[0].user_id == user["user_id"]
        assert rows[0].tenant_id == TENANT
        assert rows[0].details == {"k": "v"}
        assert rows[0].request_id == "req-gdxa-48"
    finally:
        engine.dispose()


def test_helper_returns_false_and_leaves_the_session_usable(caplog):
    """The refusal is contained: no row, a False return, a usable session.

    The engine is fresh, so ``ensure_audit_table``'s first-use commit runs
    inside this call — the exact collision the ledger entry called impossible
    to fix. It is survivable here only because that commit happens before the
    savepoint opens.
    """
    from gdx_dispatch.core.audit import audit_best_effort

    engine = _engine()
    _refuse_audit_inserts(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    user = _login()
    try:
        with SessionLocal() as db:
            with caplog.at_level("ERROR"):
                ok = audit_best_effort(
                    db,
                    action="thing_happened",
                    entity_type="thing",
                    entity_id="thing-1",
                    tenant_id=TENANT,
                    user_id=user["user_id"],
                    request=_request(user),
                )
            assert ok is False, "a caller must be able to see that the trail is missing"
            assert _session_is_usable(db), "the session must survive a refused audit write"

        assert _rows(SessionLocal, AuditLog) == []
        assert "audit_best_effort_failed" in caplog.text, "a lost trail row is never silent"
    finally:
        engine.dispose()


def test_helper_does_not_discard_work_the_caller_already_committed():
    """The backstop rollback must not eat the mutation the row was about.

    Every one of GDXA-44's six sites commits immediately before its audit call,
    so the outer transaction holds nothing but reads by the time this runs.
    """
    from gdx_dispatch.core.audit import audit_best_effort

    engine = _engine()
    _refuse_audit_inserts(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    uid = uuid4()
    try:
        with SessionLocal() as db:
            db.add(
                User(
                    id=uid, username="unlocked", email=f"{uid.hex[:8]}@example.com",
                    role="technician", company_id=TENANT, active=True,
                )
            )
            db.commit()  # the caller's mutation is already durable

            assert audit_best_effort(
                db, action="user_unlocked", entity_type="user", entity_id=str(uid),
                tenant_id=TENANT,
            ) is False

        assert len(_rows(SessionLocal, User, id=uid)) == 1, "the committed row must survive"
    finally:
        engine.dispose()


def test_helper_hardens_pending_work_so_the_precondition_is_load_bearing():
    """The boundary of the class fix, pinned so the wider sweep can read it.

    ``audit_best_effort`` commits. A caller that still has *pending* work when
    it runs gets that work hardened whether it wanted it or not — the control
    below proves the same row is discarded by a bare ``close()`` without it.
    That is why the six GDXA-44 sites qualify (all commit first) and why a site
    that does not must use ``audit_or_rollback`` before its commit instead.

    ``audit_ready_db`` does not rescue this case: it only moves
    ``ensure_audit_table``'s first-use commit out of the way.
    """
    from gdx_dispatch.core.audit import audit_best_effort, ensure_audit_table

    engine = _engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _stage_a_user(after) -> bool:
        uid = uuid4()
        with SessionLocal() as db:
            db.add(
                User(
                    id=uid, username="pending", email=f"{uid.hex[:8]}@example.com",
                    role="technician", company_id=TENANT, active=True,
                )
            )
            db.flush()  # staged, never committed by the caller
            after(db)
        return len(_rows(SessionLocal, User, id=uid)) == 1

    try:
        # Warm the guard first, exactly as audit_ready_db would.
        with SessionLocal() as db:
            ensure_audit_table(db)

        assert _stage_a_user(lambda db: None) is False, (
            "control: an uncommitted row must die with the session"
        )
        assert _stage_a_user(
            lambda db: audit_best_effort(db, action="a", entity_type="t", entity_id="1")
        ) is True, "the helper commits — a caller with pending work loses control of it"
    finally:
        engine.dispose()


def test_the_savepoint_is_load_bearing_not_decoration():
    """Delete ``begin_nested()`` and this fails: a rollback expires everything.

    The savepoint and the conditional rollback look redundant — either alone
    keeps the session usable. They are not. ``begin_nested()`` contains the
    failure *without* disturbing what the caller still holds; a rollback
    restores the session by throwing away its entire identity map. Handlers
    here read their row back through that session to build the response
    (``custom_fields._serialize_definition(defn)``), so the difference is real
    work, not bookkeeping.
    """
    from sqlalchemy import inspect as sa_inspect

    from gdx_dispatch.core.audit import audit_best_effort, ensure_audit_table

    engine = _engine()
    _refuse_audit_inserts(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    uid = uuid4()
    try:
        # Warm the guard on its own session first. In the app it is warm after
        # the first request of an engine's life; leaving it cold here would
        # measure ensure_audit_table's one-time commit instead of the savepoint.
        with SessionLocal() as warm:
            ensure_audit_table(warm)

        with SessionLocal() as db:
            db.add(
                User(
                    id=uid, username="held", email=f"{uid.hex[:8]}@example.com",
                    role="technician", company_id=TENANT, active=True,
                )
            )
            db.commit()
            held = db.execute(select(User).where(User.id == uid)).scalars().one()
            assert held.username == "held"  # loaded, not expired

            assert audit_best_effort(
                db, action="user_unlocked", entity_type="user", entity_id=str(uid)
            ) is False

            assert not sa_inspect(held).expired, (
                "the caller's loaded row must survive a contained audit failure"
            )
    finally:
        engine.dispose()


def test_the_rollback_fallback_is_load_bearing_not_decoration():
    """Delete the conditional ``db.rollback()`` and this fails.

    The savepoint only contains what happens *inside* it. A session the caller
    already deactivated — its own failed flush, before it ever got here — is
    still dead when this helper opens its savepoint, and only a rollback
    recovers it. ``is_active`` is what tells the two apart.
    """
    from gdx_dispatch.core.audit import audit_best_effort, log_audit_event_sync

    engine = _engine()
    _refuse_audit_inserts(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        with SessionLocal() as db:
            # Poison it exactly the way the old hand-rolled helpers did — a bare
            # flush against the refusing table, swallowed. Four of GDXA-44's six
            # sites still do this while wave 2 is outstanding, so a session
            # arriving here already dead is a real state, not a contrived one.
            with contextlib.suppress(Exception):
                log_audit_event_sync(
                    db, tenant_id=TENANT, user_id="u", action="earlier",
                    entity_type="thing", entity_id="0", details={},
                )
            assert db.is_active is False, "precondition: the caller broke its own session"

            assert audit_best_effort(
                db, action="a", entity_type="t", entity_id="1"
            ) is False
            assert _session_is_usable(db), "the helper must hand back a usable session"
    finally:
        engine.dispose()


def test_a_caller_with_staged_work_is_warned_not_silently_hardened(caplog):
    """The precondition is checked, not merely documented.

    ``audit_ready_db`` exists because this class of collision is easy to write
    and invisible when it happens. A warning naming the action is what makes a
    future wave-2 call site findable in the logs instead of in a postmortem.
    """
    from gdx_dispatch.core.audit import audit_best_effort

    engine = _engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    uid = uuid4()
    try:
        with SessionLocal() as db, caplog.at_level("WARNING"):
            db.add(
                User(
                    id=uid, username="staged", email=f"{uid.hex[:8]}@example.com",
                    role="technician", company_id=TENANT, active=True,
                )
            )
            assert audit_best_effort(
                db, action="thing_happened", entity_type="thing", entity_id="1"
            ) is True

        assert "audit_best_effort_caller_has_pending_work" in caplog.text
        assert "action=thing_happened" in caplog.text
    finally:
        engine.dispose()


def test_a_clean_caller_is_not_warned(caplog):
    """The detector must be quiet on the ten call sites that are correct."""
    from gdx_dispatch.core.audit import audit_best_effort

    engine = _engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    uid = uuid4()
    try:
        with SessionLocal() as db, caplog.at_level("WARNING"):
            db.add(
                User(
                    id=uid, username="clean", email=f"{uid.hex[:8]}@example.com",
                    role="technician", company_id=TENANT, active=True,
                )
            )
            db.commit()
            db.refresh(db.execute(select(User).where(User.id == uid)).scalars().one())
            assert audit_best_effort(
                db, action="user_unlocked", entity_type="user", entity_id=str(uid)
            ) is True

        assert "caller_has_pending_work" not in caplog.text
    finally:
        engine.dispose()


# ── routers/custom_fields.py — the "500 that lies" shape ─────────────────────


@pytest.fixture()
def custom_fields_client():
    from gdx_dispatch.routers import custom_fields as custom_fields_mod

    client = _build_client(
        custom_fields_mod.router, user=_login("admin"), module_keys=("jobs",)
    )
    yield client
    client.app.dependency_overrides.clear()
    client._engine.dispose()  # type: ignore[attr-defined]


_DEFN = {
    "entity_type": "job",
    "field_key": "door_model",
    "label": "Door model",
    "field_type": "text",
}


def test_custom_field_create_control(custom_fields_client):
    """Control: audit healthy, 201, one definition, one audit row."""
    from gdx_dispatch.routers.custom_fields import CustomFieldDefinition

    r = custom_fields_client.post("/api/custom-fields", json=_DEFN)
    assert r.status_code == 201, r.text
    SessionLocal = custom_fields_client.SessionLocal  # type: ignore[attr-defined]
    assert len(_rows(SessionLocal, CustomFieldDefinition, field_key="door_model")) == 1
    assert len(_rows(SessionLocal, AuditLog, action="custom_field_created")) == 1


def test_custom_field_create_survives_a_refused_audit_write(custom_fields_client):
    """The defect: today this 500s *after* committing the definition.

    The handler reads ``defn`` back through the poisoned session to build its
    response, so the swallowed ``PendingRollbackError`` resurfaces there. The
    user is told the create failed; the row is in the database.
    """
    from gdx_dispatch.routers.custom_fields import CustomFieldDefinition

    SessionLocal = custom_fields_client.SessionLocal  # type: ignore[attr-defined]
    _refuse_audit_inserts(custom_fields_client._engine)  # type: ignore[attr-defined]

    r = custom_fields_client.post("/api/custom-fields", json=_DEFN)

    assert r.status_code == 201, (
        "the definition committed; answering 500 tells the user to retry and "
        f"create a duplicate (got {r.status_code}: {r.text[:200]})"
    )
    assert r.json()["field_key"] == "door_model"
    assert len(_rows(SessionLocal, CustomFieldDefinition, field_key="door_model")) == 1
    # The trail row is genuinely lost — that is the honest outcome when the
    # audit table itself refuses, and the ERROR log is the only record of it.
    assert _rows(SessionLocal, AuditLog, action="custom_field_created") == []


def test_custom_field_create_is_not_repeated_by_a_retrying_user(custom_fields_client):
    """A 500 on a committed create is what produced duplicate rows on prod.

    Second POST of the same key now hits the uniqueness check and 409s, which
    is only reachable because the first one was honest about having created it.
    """
    from gdx_dispatch.routers.custom_fields import CustomFieldDefinition

    SessionLocal = custom_fields_client.SessionLocal  # type: ignore[attr-defined]
    _refuse_audit_inserts(custom_fields_client._engine)  # type: ignore[attr-defined]

    first = custom_fields_client.post("/api/custom-fields", json=_DEFN)
    second = custom_fields_client.post("/api/custom-fields", json=_DEFN)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert len(_rows(SessionLocal, CustomFieldDefinition, field_key="door_model")) == 1


def test_custom_fields_audit_helper_leaves_the_session_usable():
    """``custom_fields._audit`` itself, with the storage layer refusing."""
    from gdx_dispatch.routers import custom_fields as custom_fields_mod

    engine = _engine()
    _refuse_audit_inserts(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    user = _login()
    try:
        with SessionLocal() as db:
            custom_fields_mod._audit(
                db, _request(user), user,
                action="custom_field_created", entity_id="cf-1", details={"a": 1},
            )
            assert _session_is_usable(db)
    finally:
        engine.dispose()


# ── routers/admin_settings.py — the "permanent blank, HTTP 200" shape ────────


@pytest.fixture()
def admin_settings_client():
    from gdx_dispatch.routers import admin_settings as admin_settings_mod

    client = _build_client(
        admin_settings_mod.router, user=_login("admin"), module_keys=("jobs",)
    )
    yield client
    client.app.dependency_overrides.clear()
    client._engine.dispose()  # type: ignore[attr-defined]


_JURISDICTION = {"name": "Maricopa", "rate": 8.6, "is_default": False}


def test_tax_jurisdiction_create_control(admin_settings_client):
    """Control: audit healthy, 201, one jurisdiction, one audit row."""
    r = admin_settings_client.post("/api/admin/tax-jurisdictions", json=_JURISDICTION)
    assert r.status_code == 201, r.text
    SessionLocal = admin_settings_client.SessionLocal  # type: ignore[attr-defined]
    assert len(_rows(SessionLocal, TaxJurisdiction, name="Maricopa")) == 1
    assert len(_rows(SessionLocal, AuditLog, action="tax_jurisdiction_created")) == 1


def test_tax_jurisdiction_create_survives_a_refused_audit_write(admin_settings_client):
    """admin_settings returns a plain literal, so the 201 was already right.

    What was wrong is that the session went back to FastAPI deactivated. Here
    that is invisible (nothing else uses it before the request ends), so this
    test pins the contract rather than catching the defect — the session-level
    proof is the helper test below.
    """
    SessionLocal = admin_settings_client.SessionLocal  # type: ignore[attr-defined]
    _refuse_audit_inserts(admin_settings_client._engine)  # type: ignore[attr-defined]

    r = admin_settings_client.post("/api/admin/tax-jurisdictions", json=_JURISDICTION)

    assert r.status_code == 201, r.text
    assert r.json()["ok"] is True
    assert len(_rows(SessionLocal, TaxJurisdiction, name="Maricopa")) == 1
    assert _rows(SessionLocal, AuditLog, action="tax_jurisdiction_created") == []


def test_admin_settings_audit_helper_leaves_the_session_usable():
    """``admin_settings._audit`` itself, with the storage layer refusing.

    This is the admin_settings half of the defect: the helper swallowed the
    refusal and returned a session on which nothing further could run.
    """
    from gdx_dispatch.routers import admin_settings as admin_settings_mod

    engine = _engine()
    _refuse_audit_inserts(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    user = _login()
    try:
        with SessionLocal() as db:
            admin_settings_mod._audit(
                db, request=_request(user), user=user, action="user_unlocked",
                entity_type="user", entity_id=str(uuid4()),
            )
            assert _session_is_usable(db)
    finally:
        engine.dispose()


# ── the same thing on a real Postgres ────────────────────────────────────────
#
# SQLite and Postgres disagree about savepoints in ways that matter here (the
# release of an outermost SAVEPOINT commits on one and not the other), so
# "proven on SQLite" is not proven. Skips without a reachable server, exactly
# like every other PG arm in this suite — and FAILS under CI, which has one.


def test_postgres_control_and_refusal(pg_test_engine):
    """Control then refusal, against PG 15 with a real BEFORE INSERT trigger.

    The caller's mutation is a scratch table created here rather than an ORM
    model: the template database is loaded from ``structure.sql``, which has
    drifted from the ORM (``users`` is missing ``shift_start``), and neither
    that drift nor ``create_all`` over the template is what this test is about.
    ``audit_logs`` itself is present and current in the template.
    """
    from gdx_dispatch.core.audit import audit_best_effort

    SessionLocal = sessionmaker(bind=pg_test_engine, autoflush=False, autocommit=False)
    with pg_test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE caller_work (id text PRIMARY KEY)"))

    def _caller_rows() -> int:
        with pg_test_engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM caller_work")).scalar_one()

    with SessionLocal() as db:
        db.execute(text("INSERT INTO caller_work (id) VALUES ('w1')"))
        db.commit()  # the caller's mutation, durable before the audit
        assert audit_best_effort(
            db, action="pg_control", entity_type="thing", entity_id="w1",
            tenant_id=TENANT, user_id="u",
        ) is True

    assert len(_rows(SessionLocal, AuditLog, action="pg_control")) == 1

    with pg_test_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE FUNCTION audit_logs_refuse() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'audit storage refuses this row'; END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER audit_logs_refuse_insert
                    BEFORE INSERT ON audit_logs
                    FOR EACH ROW EXECUTE FUNCTION audit_logs_refuse()
                """
            )
        )

    with SessionLocal() as db:
        db.execute(text("INSERT INTO caller_work (id) VALUES ('w2')"))
        db.commit()
        assert audit_best_effort(
            db, action="pg_refused", entity_type="thing", entity_id="w2",
            tenant_id=TENANT, user_id="u",
        ) is False
        assert _session_is_usable(db), "PG deactivates the transaction too (25P02)"

    assert _rows(SessionLocal, AuditLog, action="pg_refused") == []
    assert _caller_rows() == 2, "both committed rows survived the refused audit"
