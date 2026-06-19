"""
Test 07 — GDPR tenant-level deletion workflow.
Covers: admin auth guard, 404 for missing tenant, deleted_at set,
idempotency (409/already_deleted), Stripe cancel mock, DB drop step.

All tests use isolated in-memory SQLite control-plane DBs — no real Postgres
or Stripe calls are made.

SKIPPED: The gdx_dispatch.core.gdpr_router module (with gdpr_tenant_delete,
_cancel_stripe_subscription, _drop_tenant_database, _revoke_qb_tokens) was
planned but never implemented. The actual GDPR router lives at
gdx_dispatch/routers/gdpr.py with a different API surface. These tests need to be
rewritten to target the real router.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.routers

pytest.skip(
    "gdx_dispatch.core.gdpr_router does not exist — tests must be rewritten for gdx_dispatch.routers.gdpr",
    allow_module_level=True,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ctrl_db():
    """Fresh isolated SQLite control-plane DB for each test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    yield db
    db.close()
    engine.dispose()


def _make_tenant(ctrl_db, *, stripe_sub_id: str | None = "sub_test123", deleted_at=None) -> Tenant:
    t = Tenant(
        slug="acme",
        name="Acme Corp",   # plaintext SQLite URL — dev mode
        stripe_subscription_id=stripe_sub_id,
        subscription_status="active",
        deleted_at=deleted_at,
    )
    ctrl_db.add(t)
    ctrl_db.commit()
    ctrl_db.refresh(t)
    return t


# ---------------------------------------------------------------------------
# test_gdpr_tenant_delete_requires_admin
# ---------------------------------------------------------------------------

def test_gdpr_tenant_delete_requires_admin():
    """The endpoint must be declared with the admin role dependency."""

    # Inspect the route's dependencies for require_role("admin")
    getattr(gdpr_tenant_delete, "__wrapped__", gdpr_tenant_delete)
    # FastAPI stores endpoint dependencies on the router, not the function directly.
    # Verify indirectly: calling the function without an admin role raises 403.
    from fastapi import HTTPException
    mock_db = MagicMock()
    mock_db.execute.return_value.scalar_one_or_none.return_value = None
    # We patch require_role to simulate a non-admin call raising 403
    with patch("gdx_dispatch.core.gdpr_router.require_role") as mock_require:
        mock_require.return_value = MagicMock(side_effect=HTTPException(status_code=403, detail="Insufficient role"))
        # The real enforcement happens via FastAPI's dependency injection at request time.
        # Here we simply assert require_role("admin") is referenced in the endpoint deps.
        import inspect
        src = inspect.getsource(gdpr_tenant_delete)
        assert 'require_role("admin")' in src


# ---------------------------------------------------------------------------
# test_gdpr_tenant_delete_nonexistent_returns_404
# ---------------------------------------------------------------------------

def test_gdpr_tenant_delete_nonexistent_returns_404(ctrl_db):
    from fastapi import HTTPException
    missing_id = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        gdpr_tenant_delete(missing_id, control_db=ctrl_db)
    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# test_gdpr_tenant_delete_marks_deleted_at
# ---------------------------------------------------------------------------

def test_gdpr_tenant_delete_marks_deleted_at(ctrl_db):
    tenant = _make_tenant(ctrl_db, stripe_sub_id=None)
    tenant_id = str(tenant.id)

    with patch("gdx_dispatch.core.gdpr_router._cancel_stripe_subscription") as mock_stripe, \
         patch("gdx_dispatch.core.gdpr_router._revoke_qb_tokens") as mock_qb, \
         patch("gdx_dispatch.core.gdpr_router._drop_tenant_database") as mock_drop:
        mock_stripe.return_value = {"stripe_cancelled": False, "reason": "no_subscription_id"}
        mock_qb.return_value = {"qb_tokens_revoked": True, "rows_deleted": 0}
        mock_drop.return_value = {"db_dropped": False, "reason": "sqlite_skipped"}

        result = gdpr_tenant_delete(tenant_id, control_db=ctrl_db)

    assert result["status"] == "deleted"
    assert result["tenant_id"] == tenant_id
    assert "deleted_at" in result

    # Verify persisted in DB
    ctrl_db.expire_all()
    refreshed = ctrl_db.execute(select(Tenant).where(Tenant.id == tenant.id)).scalar_one()
    assert refreshed.deleted_at is not None
    assert refreshed.subscription_status == "canceled"


# ---------------------------------------------------------------------------
# test_gdpr_tenant_delete_is_idempotent
# ---------------------------------------------------------------------------

def test_gdpr_tenant_delete_is_idempotent(ctrl_db):
    """Second call on an already-deleted tenant returns already_deleted (200, not error)."""
    already_deleted_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tenant = _make_tenant(ctrl_db, stripe_sub_id=None, deleted_at=already_deleted_at)
    tenant_id = str(tenant.id)

    result = gdpr_tenant_delete(tenant_id, control_db=ctrl_db)

    assert result["status"] == "already_deleted"
    assert result["tenant_id"] == tenant_id
    assert "deleted_at" in result


# ---------------------------------------------------------------------------
# test_gdpr_tenant_delete_stripe_cancel_called
# ---------------------------------------------------------------------------

def test_gdpr_tenant_delete_stripe_cancel_called(ctrl_db):
    """Stripe cancellation helper is invoked with the correct subscription ID."""
    tenant = _make_tenant(ctrl_db, stripe_sub_id="sub_abc999")
    tenant_id = str(tenant.id)

    with patch("gdx_dispatch.core.gdpr_router._cancel_stripe_subscription") as mock_stripe, \
         patch("gdx_dispatch.core.gdpr_router._revoke_qb_tokens") as mock_qb, \
         patch("gdx_dispatch.core.gdpr_router._drop_tenant_database") as mock_drop:
        mock_stripe.return_value = {"stripe_cancelled": True, "subscription_id": "sub_abc999"}
        mock_qb.return_value = {"qb_tokens_revoked": True, "rows_deleted": 0}
        mock_drop.return_value = {"db_dropped": False, "reason": "sqlite_skipped"}

        result = gdpr_tenant_delete(tenant_id, control_db=ctrl_db)

    mock_stripe.assert_called_once_with("sub_abc999", tenant_id, ctrl_db)
    assert result["steps"]["stripe"]["stripe_cancelled"] is True
    assert result["steps"]["stripe"]["subscription_id"] == "sub_abc999"


def test_gdpr_stripe_cancel_helper_calls_stripe_api():
    """Unit-test _cancel_stripe_subscription directly with a mocked stripe module."""
    mock_db = MagicMock()

    with patch.dict("os.environ", {"STRIPE_SECRET_KEY": "sk_test_dummy"}), patch("stripe.Subscription") as mock_sub:
        mock_sub.cancel.return_value = {"status": "canceled"}
        # Re-import stripe inside the helper context
        with patch("gdx_dispatch.core.gdpr_router.__builtins__", __builtins__):
            result = _cancel_stripe_subscription("sub_xyz", "tenant-1", mock_db)

    # Whether stripe is installed or not, the helper must return a dict with stripe_cancelled key
    assert "stripe_cancelled" in result


# ---------------------------------------------------------------------------
# test_gdpr_tenant_delete_db_dropped
# ---------------------------------------------------------------------------

def test_gdpr_tenant_delete_db_dropped(ctrl_db):
    """_drop_tenant_database is invoked and its result appears in the response steps."""
    tenant = _make_tenant(ctrl_db, stripe_sub_id=None)
    tenant_id = str(tenant.id)

    with patch("gdx_dispatch.core.gdpr_router._cancel_stripe_subscription") as mock_stripe, \
         patch("gdx_dispatch.core.gdpr_router._revoke_qb_tokens") as mock_qb, \
         patch("gdx_dispatch.core.gdpr_router._drop_tenant_database") as mock_drop:
        mock_stripe.return_value = {"stripe_cancelled": False, "reason": "no_subscription_id"}
        mock_qb.return_value = {"qb_tokens_revoked": True, "rows_deleted": 0}
        mock_drop.return_value = {"db_dropped": True, "db_name": "acme_db"}

        result = gdpr_tenant_delete(tenant_id, control_db=ctrl_db)

    mock_drop.assert_called_once()
    assert result["steps"]["db_drop"]["db_dropped"] is True
    assert result["steps"]["db_drop"]["db_name"] == "acme_db"


def test_gdpr_drop_sqlite_is_skipped():
    """_drop_tenant_database skips gracefully for SQLite URLs (dev/test)."""
    result = _drop_tenant_database("sqlite:///./test.db", "tenant-1", "acme")
    assert result["db_dropped"] is False
    assert result["reason"] == "sqlite_skipped"


def test_gdpr_revoke_qb_tokens_table_missing():
    """_revoke_qb_tokens returns a safe error dict when qb_token_store table doesn't exist."""
    result = _revoke_qb_tokens("sqlite://", "tenant-1")
    # SQLite won't have qb_token_store — should fail gracefully
    assert "qb_tokens_revoked" in result
    # Either succeeds with 0 rows or fails gracefully — either is acceptable
    assert isinstance(result.get("qb_tokens_revoked"), bool)
