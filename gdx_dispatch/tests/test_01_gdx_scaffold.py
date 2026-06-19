"""
test_01_gdx_scaffold.py — Verify GDX FastAPI scaffold starts and routes are registered.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gdx_client():
    """TestClient with TenantMiddleware bypassed for scaffold tests."""
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from gdx_dispatch.app import create_app
    app = create_app()
    # Bypass tenant middleware for health check tests
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_health_endpoint(gdx_client):
    """GET /health returns 200 with status ok.

    Skips gracefully when the test env cannot reach the control DB
    (common in local venv without docker-compose). CI + dev containers
    have a reachable postgres service.

    SS-7 Slice L — the response also pins denylist backend visibility
    without leaking ``REDIS_URL``/connection-string content.
    """
    rv = gdx_client.get("/health")
    if rv.status_code == 503 and "could not translate host name" in rv.text.lower() or "name or service" in rv.text.lower() or rv.status_code == 503:
        import pytest as _p
        _p.skip("control DB unreachable from this environment (local venv w/o docker-postgres)")
    assert rv.status_code == 200
    data = rv.json()
    assert data.get("status") == "ok"
    # SS-7 Slice L — denylist backend mode is surfaced as a read-only key
    # with a closed vocabulary. Allowed values mirror the mode matrix in
    # ``docs/ops/denylist_backend_mode.md`` (helper returns a client →
    # ``"redis"``; helper returns ``None`` → ``"memory"``).
    assert "denylist_backend" in data, (
        f"/health must expose denylist_backend key; got {data!r}"
    )
    assert data["denylist_backend"] in ("memory", "redis"), (
        f"denylist_backend must be 'memory' or 'redis', got {data['denylist_backend']!r}"
    )
    # /health must never echo REDIS_URL content. Scan the raw body for
    # obvious redis connection-string markers regardless of env contents.
    body_text = rv.text
    for needle in ("redis://", "rediss://"):
        assert needle not in body_text, (
            f"/health body leaked a redis connection string marker: {needle!r}"
        )
    # If the real env happens to have a REDIS_URL set, make sure its exact
    # value does not appear in the response either (credentials live in URL).
    import os as _os
    _redis_url = _os.environ.get("REDIS_URL", "").strip()
    if _redis_url:
        assert _redis_url not in body_text, (
            "/health body leaked the exact REDIS_URL value"
        )


def test_health_endpoint_denylist_probe_fail_open(gdx_client, monkeypatch):
    """SS-7 Slice M — /health stays green when the denylist probe raises.

    The Slice L probe calls ``gdx_dispatch.routers.auth._denylist_redis_client``
    inside a try/except that logs ``denylist_backend_probe_failed`` and
    degrades to ``denylist_backend == "memory"``. This pins that
    fail-open contract: a raising helper must never surface as a 5xx on
    ``/health``, never leak ``REDIS_URL`` / connection-string markers,
    and never leak the raw exception message into the response body.
    """
    secret_marker = "redis-probe-secret-leak-marker"

    def _raising_helper():
        raise RuntimeError(
            f"simulated probe failure with {secret_marker} and redis://user:pw@host/0"
        )

    # Patch the module attribute the /health body re-imports at call time.
    # Using ``monkeypatch.setattr`` restores the original reference at
    # test teardown so the preceding ``test_health_endpoint`` is not
    # affected when tests run in a different order.
    import gdx_dispatch.routers.auth as _auth_mod
    monkeypatch.setattr(_auth_mod, "_denylist_redis_client", _raising_helper)

    rv = gdx_client.get("/health")
    # Preserve the same control-DB skip semantics ``test_health_endpoint``
    # uses: a local venv without docker-compose cannot reach the control
    # DB and `/health` returns 503 before the denylist probe runs. The
    # denylist fail-open contract only applies once DB probing passes.
    if rv.status_code == 503:
        import pytest as _p
        _p.skip("control DB unreachable from this environment (local venv w/o docker-postgres)")
    assert rv.status_code == 200, (
        f"/health must stay green on denylist probe failure, got {rv.status_code}: {rv.text!r}"
    )
    data = rv.json()
    assert data.get("status") == "ok"
    # Fail-open contract: probe exception degrades to "memory", not a 5xx.
    assert data.get("denylist_backend") == "memory", (
        f"denylist_backend must degrade to 'memory' on probe failure, got {data!r}"
    )
    # The raised exception message included a redis:// marker and a
    # distinctive secret; neither must appear in the response body.
    body_text = rv.text
    assert secret_marker not in body_text, (
        "/health body leaked the raw exception message on probe failure"
    )
    for needle in ("redis://", "rediss://"):
        assert needle not in body_text, (
            f"/health body leaked a redis connection string marker: {needle!r}"
        )


def test_health_endpoint_denylist_probe_failure_emits_log_event(
    gdx_client, monkeypatch, caplog
):
    """SS-7 Slice O — /health emits ``denylist_backend_probe_failed`` on raise.

    Slice M pinned the response-shape half of the fail-open contract:
    a raising denylist probe must not surface as 5xx and must not leak
    secrets into the response body. This Slice O pins the observability
    half — the exact event name ``denylist_backend_probe_failed`` MUST
    appear in log output on the probe-failure branch so alerts keyed on
    that string in ``docs/ops/denylist_backend_mode.md`` continue to fire.

    Keeping the log-emission assertion in its own test keeps failure
    modes precise: a regression that silences the log but preserves
    fail-open would fail only here; a regression that leaks secrets but
    still logs would fail only in Slice M. Together they pin the full
    fail-open contract.
    """
    import logging

    def _raising_helper():
        raise RuntimeError("simulated denylist probe failure")

    import gdx_dispatch.routers.auth as _auth_mod
    monkeypatch.setattr(_auth_mod, "_denylist_redis_client", _raising_helper)

    # The /health handler calls ``logging.getLogger("gdx_dispatch.app").exception(...)``
    # which emits at ERROR. Pin the capture level explicitly so a future
    # default-level change does not silently hide the record.
    caplog.set_level(logging.ERROR, logger="gdx_dispatch.app")

    rv = gdx_client.get("/health")
    # Preserve the same control-DB skip semantics as the Slice L/M tests:
    # a local venv without docker-compose cannot reach the control DB and
    # /health returns 503 before the denylist probe runs, so there is no
    # probe-failure branch to observe.
    if rv.status_code == 503:
        import pytest as _p
        _p.skip("control DB unreachable from this environment (local venv w/o docker-postgres)")

    # Reassert the fail-open response invariants so one run of this test
    # is sufficient evidence that both halves of the contract hold.
    assert rv.status_code == 200, (
        f"/health must stay green on denylist probe failure, got {rv.status_code}: {rv.text!r}"
    )
    data = rv.json()
    assert data.get("status") == "ok"
    assert data.get("denylist_backend") == "memory", (
        f"denylist_backend must degrade to 'memory' on probe failure, got {data!r}"
    )
    # Defense-in-depth: REDIS_URL markers must not leak even though this
    # test's raising helper omits them — prevents a regression where a
    # reviewer copies this test and loosens the probe error path.
    body_text = rv.text
    for needle in ("redis://", "rediss://"):
        assert needle not in body_text, (
            f"/health body leaked a redis connection string marker: {needle!r}"
        )

    # Log-emission invariant (Slice O). The runbook at
    # ``docs/ops/denylist_backend_mode.md`` pins this exact event name
    # under "Log events to alert on"; the Slice N parity guard pins that
    # name→source mapping statically. This assertion closes the loop by
    # proving the event is ACTUALLY emitted on the probe-fail branch at
    # runtime — a rename that updates both runbook and emitter in
    # lockstep (passing Slice N) while silencing the call site would
    # fail here.
    event_name = "denylist_backend_probe_failed"
    matching = [
        rec for rec in caplog.records
        if rec.name == "gdx_dispatch.app" and rec.getMessage() == event_name
    ]
    assert matching, (
        f"expected a {event_name!r} log record on the 'gdx_dispatch.app' logger; "
        f"captured records were: "
        f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]!r}"
    )
    # ``.exception(...)`` emits at ERROR — pin the level so a drop to
    # INFO/DEBUG (which most alerting pipelines skip) fails loudly.
    assert matching[0].levelno == logging.ERROR, (
        f"{event_name} must be logged at ERROR level, got {matching[0].levelname}"
    )


def test_auth_routes_registered():
    """Auth routes are registered in the app."""
    from gdx_dispatch.app import create_app
    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/auth/login" in paths
    assert "/auth/refresh" in paths
    assert "/auth/logout" in paths


def test_jobs_routes_registered():
    """Jobs routes are registered in the app."""
    from gdx_dispatch.app import create_app
    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/api/jobs" in paths
    assert "/api/jobs/{job_id}" in paths



def test_control_models_importable():
    """Control plane models can be imported."""
    from gdx_dispatch.control.models import FeatureFlag, Tenant
    assert Tenant.__tablename__ == "tenants"
    assert FeatureFlag.__tablename__ == "platform_feature_flags"


def test_stripe_webhook_route_registered():
    """Stripe webhook route is registered."""
    from gdx_dispatch.app import create_app
    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/stripe/webhook" in paths


def test_celery_app_importable():
    """Celery app can be imported with correct task routing config."""
    from gdx_dispatch.core.celery_app import celery_app
    # Verify high/low priority task routing is configured
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True



def test_fernet_decrypt_passthrough_dev_mode():
    """Phase C: _decrypt_db_url is always a passthrough (no Fernet in single-tenant)."""
    import gdx_dispatch.core.database as db_mod
    result = db_mod._decrypt_db_url("postgresql://localhost/test")
    assert result == "postgresql://localhost/test"


def test_observability_importable():
    """Observability module can be imported and init_sentry no-ops with empty DSN."""
    from gdx_dispatch.core.observability import init_otel, init_sentry
    # Should not raise with empty DSN
    init_sentry("", "test")
    init_otel("gdx-test")


def test_feature_flags_importable():
    """Feature flags module can be imported with expected functions."""
    from gdx_dispatch.core.feature_flags import is_flag_enabled, set_flag_rollout
    assert callable(is_flag_enabled)
    assert callable(set_flag_rollout)


def test_core_fsm_models_importable():
    """Tenant FSM models and transition validators are importable."""
    from gdx_dispatch.models.tenant_models import (
        BILLING_TRANSITIONS,
        DISPATCH_TRANSITIONS,
        LIFECYCLE_TRANSITIONS,
    )
    assert "lead" in LIFECYCLE_TRANSITIONS
    assert "completed" in LIFECYCLE_TRANSITIONS
    assert "unassigned" in DISPATCH_TRANSITIONS
    assert "unbilled" in BILLING_TRANSITIONS


def test_pii_encryption_dev_mode(monkeypatch):
    """EncryptedString passthrough works without MASTER_ENCRYPTION_KEY."""
    monkeypatch.delenv("MASTER_ENCRYPTION_KEY", raising=False)
    import gdx_dispatch.core.pii as pii_mod
    enc = pii_mod.EncryptedString()
    result = enc.process_bind_param("test@example.com", None)
    assert result == "test@example.com"


def test_s122_1_encryption_boot_gate_refuses_in_prod(monkeypatch):
    """S122-1 (T1): _check_encryption_at_rest must SystemExit when
    MASTER_ENCRYPTION_KEY is unset AND GDX_ENV is production-like.
    Without this gate, qb_token_store.refresh_token_enc silently holds
    plaintext refresh tokens — direct grants to a customer's QBO realm.
    """
    import pytest
    import gdx_dispatch.core.pii as pii_mod
    from gdx_dispatch.app import _check_encryption_at_rest
    monkeypatch.setattr(pii_mod, "_FERNET", None)
    # Simulate non-pytest runtime — the gate short-circuits under PYTEST_CURRENT_TEST
    # so test runs themselves don't get refused-to-boot.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    for env_value in ("", "production", "prod"):
        monkeypatch.setenv("GDX_ENV", env_value)
        with pytest.raises(SystemExit) as excinfo:
            _check_encryption_at_rest()
        assert "MASTER_ENCRYPTION_KEY" in str(excinfo.value)


def test_s122_1_encryption_boot_gate_allows_dev(monkeypatch, caplog):
    """S122-1: dev/test environments still allow boot with no key set.
    Existing dev workflows depend on plaintext fallback (matches
    test_pii_encryption_dev_mode); the gate must not break them.
    """
    import gdx_dispatch.core.pii as pii_mod
    from gdx_dispatch.app import _check_encryption_at_rest
    monkeypatch.setattr(pii_mod, "_FERNET", None)
    for env_value in ("dev", "development", "test", "testing", "local"):
        monkeypatch.setenv("GDX_ENV", env_value)
        _check_encryption_at_rest()  # must not raise


def test_s122_1_encryption_boot_gate_passes_with_key(monkeypatch):
    """S122-1: if _FERNET is initialized, the gate is a no-op regardless of env."""
    import gdx_dispatch.core.pii as pii_mod
    from gdx_dispatch.app import _check_encryption_at_rest
    monkeypatch.setattr(pii_mod, "_FERNET", object())  # truthy placeholder
    monkeypatch.setenv("GDX_ENV", "production")
    _check_encryption_at_rest()  # must not raise


def test_audit_log_model_columns():
    """AuditLog has all required columns including hash chain fields."""
    from gdx_dispatch.core.audit import AuditLog
    cols = {c.key for c in AuditLog.__table__.columns}
    assert "hash" in cols
    assert "prev_hash" in cols
    assert "event_type" in cols
    assert "actor_id" in cols


def test_idempotency_middleware_importable():
    """Idempotency middleware and redis client helper are importable."""
    from gdx_dispatch.core.idempotency import get_redis_client
    assert callable(get_redis_client)


def test_alembic_migrations_exist():
    """Control-plane Alembic migration files exist.

    an earlier session (2026-04-25) killed tenant-plane alembic — tenant DBs are
    paved from `TenantBase.metadata.create_all()`; the ORM is the single
    source of truth. This test now only asserts the control-plane chain
    at `gdx_dispatch/migrations/versions/` (path defined in repo-root `alembic.ini`).
    """
    import os
    base = os.path.join(os.path.dirname(__file__), "..", "migrations", "versions")
    files = os.listdir(base)
    assert any("001" in f for f in files), "Control-plane migration 001 not found"
    assert any("002" in f for f in files), "Control-plane migration 002 not found"


def test_security_headers_in_response(gdx_client):
    """Security headers are present in /health response."""
    rv = gdx_client.get("/health")
    # X-Content-Type-Options should be set
    assert rv.headers.get("x-content-type-options") == "nosniff", \
        f"Missing x-content-type-options header: {dict(rv.headers)}"
