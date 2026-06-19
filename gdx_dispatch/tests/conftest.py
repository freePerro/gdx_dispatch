"""
GDX test configuration.
Each test file gets isolated SQLite in-memory databases — no shared state.
"""
import os
import sqlite3
from datetime import date, datetime
from functools import reduce
from operator import or_

# gdx_dispatch/routers/auth.py refuses to import without a JWT signing key configured.
# For tests we give it a deterministic HS256 secret (≥32 bytes) BEFORE any
# test module imports auth. Production must set RS_PRIVATE_KEY+RS_PUBLIC_KEY
# or a real JWT_SECRET — this fallback is test-only.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety")

# Single-tenant pin: GDXDispatch resolves exactly one tenant from GDX_TENANT_ID
# (see gdx_dispatch.core.tenant.single_tenant). The retained control-plane machinery
# (platform audit, OAuth tenant binding, RLS) is UUID-typed, so the pinned id
# must be a real UUID — not the bare "gdx" slug fallback. This canonical test
# UUID is the value the suite already standardizes on as GDX_UUID across the
# oauth2/mcp test modules. Production sets GDX_TENANT_ID to GDX's real company id.
os.environ.setdefault("GDX_TENANT_ID", "11111111-1111-1111-1111-111111111111")

# ---------------------------------------------------------------------------
# SS-12A bootstrap heartbeat (env-gated, inert unless SS12A_BOOTSTRAP_LOG set)
# ---------------------------------------------------------------------------
# Codex-side replay of the SS-12A observer under pytest's default fd-capture
# mode produces empty stdout/stderr when the process is SIGKILL'd by
# timeout(1) — the capture pipe buffer dies with the process. This heartbeat
# bypasses pytest capture by writing each checkpoint to an append-only
# on-disk log via low-level os.open/os.write/os.close, so the log survives
# SIGKILL (kernel retains the data after os.close returns). Inert unless
# SS12A_BOOTSTRAP_LOG is set to a target path.
import time as _ss12a_time


def _ss12a_bootstrap_log(label: str) -> None:
    path = os.environ.get("SS12A_BOOTSTRAP_LOG")
    if not path:
        return
    try:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, f"{_ss12a_time.time():.6f} {label}\n".encode())
        finally:
            os.close(fd)
    except Exception:
        # Heartbeat must never break a test run.
        pass


_ss12a_bootstrap_log("conftest_import_reached")


import pytest
from sqlalchemy import create_engine, event


def pytest_configure(config):
    # schemathesis 4.15.1 xdist plugin crashes on worker shutdown
    # (workeroutput AttributeError). Unregister it — we don't need
    # xdist report aggregation for schemathesis.
    plugin = config.pluginmanager.get_plugin("schemathesis-xdist")
    if plugin is not None:
        config.pluginmanager.unregister(plugin, "schemathesis-xdist")


def pytest_sessionstart(session):  # noqa: ARG001
    _ss12a_bootstrap_log("pytest_sessionstart")


def pytest_collection_finish(session):  # noqa: ARG001
    _ss12a_bootstrap_log("pytest_collection_finish")


def _ss12a_is_observer_item(item) -> bool:
    return "test_01_gdx_scaffold_hang_capture.py" in str(getattr(item, "fspath", ""))


def pytest_runtest_setup(item):
    if _ss12a_is_observer_item(item):
        _ss12a_bootstrap_log(f"runtest_setup:{item.nodeid}")


def pytest_runtest_call(item):
    if _ss12a_is_observer_item(item):
        _ss12a_bootstrap_log(f"runtest_call:{item.nodeid}")


def pytest_runtest_teardown(item, nextitem):  # noqa: ARG001
    if _ss12a_is_observer_item(item):
        _ss12a_bootstrap_log(f"runtest_teardown:{item.nodeid}")
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Register sqlite3 datetime adapters (required since Python 3.12, silences
# "The default datetime adapter is deprecated" DeprecationWarning)
sqlite3.register_adapter(datetime, lambda v: v.isoformat())
sqlite3.register_adapter(date, lambda v: v.isoformat())
sqlite3.register_converter("timestamp", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_converter("date", lambda b: date.fromisoformat(b.decode()))


def _patch_sqlalchemy_typing_for_py314() -> None:
    """Work around SQLAlchemy typing helper incompatibility on Python 3.14."""
    try:
        import sqlalchemy.util.typing as sa_typing
    except Exception:
        return

    original = getattr(sa_typing, "make_union_type", None)
    if original is None:
        return

    def _make_union_type_compat(*types):
        if not types:
            raise TypeError("make_union_type() requires at least 1 type")
        try:
            return reduce(or_, types)
        except Exception:
            return types[0]

    sa_typing.make_union_type = _make_union_type_compat


_patch_sqlalchemy_typing_for_py314()


# ---------------------------------------------------------------------------
# Cross-test isolation: reset all in-memory state between test functions
# ---------------------------------------------------------------------------

def _reset_all_in_memory_state() -> None:
    """Reset all module-level in-memory stores to prevent cross-test pollution."""
    # Pricing module
    try:
        from gdx_dispatch.routers.pricing import reset_pricing_state
        reset_pricing_state()
    except Exception:
        pass

    # Communications module
    try:
        from gdx_dispatch.routers import communications
        communications.reset_state()
    except Exception:
        pass

    # Onboarding module
    try:
        from gdx_dispatch.core.onboarding import _mem_store
        _mem_store.clear()
    except Exception:
        pass

    # Push notifications
    try:
        from gdx_dispatch.core.push_notifications import _subscriptions
        _subscriptions.clear()
    except Exception:
        pass

    # Superadmin impersonation
    try:
        from gdx_dispatch.core.superadmin import _impersonation_tokens
        _impersonation_tokens.clear()
    except Exception:
        pass

    # Circuit breaker lru_cache
    try:
        from gdx_dispatch.core.circuit_breaker import get_redis_client
        get_redis_client.cache_clear()
    except Exception:
        pass

    # AI router singletons
    try:
        from gdx_dispatch.core.ai_router import reset_ai_singletons
        reset_ai_singletons()
    except Exception:
        pass


@pytest.fixture(autouse=True, scope="function")
def _reset_module_state():
    """Clear all in-memory state before and after each test function."""
    _reset_all_in_memory_state()
    yield
    _reset_all_in_memory_state()


# ---------------------------------------------------------------------------
# Fresh DB factory
# ---------------------------------------------------------------------------

def make_fresh_db():
    """Create a fully isolated in-memory SQLite DB with all tenant tables."""
    import gdx_dispatch.models  # noqa: F401 — registers ALL tenant models on metadata
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models.tenant_models import Base as TenantModelsBase
    from gdx_dispatch.modules.equipment.models import CustomerEquipment, EquipmentServiceHistory
    from gdx_dispatch.modules.fleet.models import Vehicle, VehicleServiceRecord
    from gdx_dispatch.modules.inventory.models import JobPart, Part
    from gdx_dispatch.modules.timeclock.models import TimeClock

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create all tables — each call gets a brand new in-memory DB
    TenantModelsBase.metadata.create_all(engine, checkfirst=True)
    TenantBase.metadata.create_all(engine, checkfirst=True)

    # Phase C: platform_consumer_audit (SS28) moved to the single app DB.
    # ConsumerAuditMiddleware now uses SessionLocal, so the table must exist.
    try:
        from gdx_dispatch.models.platform_ss28_additions import SS28Base
        SS28Base.metadata.create_all(engine, checkfirst=True)
    except Exception:
        pass
    for tbl in [
        Part.__table__,
        JobPart.__table__,
        TimeClock.__table__,
        CustomerEquipment.__table__,
        EquipmentServiceHistory.__table__,
        Vehicle.__table__,
        VehicleServiceRecord.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)

    # Webhook-related tables (AIAction DLQ, etc.)
    try:
        from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint
        AIAction.__table__.create(bind=engine, checkfirst=True)
        WebhookEndpoint.__table__.create(bind=engine, checkfirst=True)
        WebhookDelivery.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass

    # QB webhook dedup table
    try:
        from gdx_dispatch.modules.quickbooks.webhook_models import QBWebhookEvent
        QBWebhookEvent.__table__.create(bind=engine, checkfirst=True)
    except ImportError:
        pass

    # Sprint 5 tables — imported lazily so missing modules don't break earlier tests
    _sprint5_tables = []
    try:
        from gdx_dispatch.modules.distributor.models import DealerOrder, DistributorAnalytic
        from gdx_dispatch.modules.distributor.onboarding import DealerInvitation
        _sprint5_tables += [DealerOrder.__table__, DistributorAnalytic.__table__, DealerInvitation.__table__]
    except ImportError:
        pass
    try:
        from gdx_dispatch.modules.wholesale.models import CatalogItem, ChannelAnalytic, PricingTier
        _sprint5_tables += [CatalogItem.__table__, PricingTier.__table__, ChannelAnalytic.__table__]
    except ImportError:
        pass
    try:
        from gdx_dispatch.modules.gps_dispatch.models import DispatchRoute, TechnicianLocation
        _sprint5_tables += [TechnicianLocation.__table__, DispatchRoute.__table__]
    except ImportError:
        pass
    try:
        from gdx_dispatch.modules.ai_health_score.models import TenantHealthScore
        _sprint5_tables.append(TenantHealthScore.__table__)
    except ImportError:
        pass
    try:
        from gdx_dispatch.core.health_score import TenantHealthLog
        _sprint5_tables.append(TenantHealthLog.__table__)
    except ImportError:
        pass
    try:
        from gdx_dispatch.modules.reporting.models import SavedReport
        _sprint5_tables.append(SavedReport.__table__)
    except ImportError:
        pass
    try:
        from gdx_dispatch.core.ai_quote import QuoteTemplate
        _sprint5_tables.append(QuoteTemplate.__table__)
    except ImportError:
        pass
    try:
        from gdx_dispatch.core.parts_pricing import PartPrice
        _sprint5_tables.append(PartPrice.__table__)
    except ImportError:
        pass
    for tbl in _sprint5_tables:
        tbl.create(bind=engine, checkfirst=True)

    # Contractors module tables
    try:
        from gdx_dispatch.modules.contractors.models import Contractor, ContractorAssignment
        Contractor.__table__.create(bind=engine, checkfirst=True)
        ContractorAssignment.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass

    # Next-action queue table
    try:
        from gdx_dispatch.core.next_action import NextAction
        NextAction.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass

    return engine


@pytest.fixture
def tenant_db():
    """Isolated tenant DB for test_02 e2e tests."""
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def control_db():
    """Isolated control plane DB for test_02 e2e tests + SS-5 platform harness.

    Importing ``gdx_dispatch.models.platform`` and ``gdx_dispatch.models.platform_extensions``
    here (not just at module top) ensures their mapper classes are registered
    against ``ControlBase.metadata`` before ``create_all`` runs — SS-5
    factories assume every platform table exists.

    Supports two modes:

    - Default (SQLite in-memory): fast, per-test isolation via ``StaticPool``,
      every test gets a fresh DB. Used by the standard pytest runs.
    - PG integration gate (SS-5 Slice C): set ``GDX_TEST_CONTROL_DB_URL`` to a
      PostgreSQL URL pre-populated by alembic. The session is SAVEPOINT-wrapped
      inside an outer BEGIN so handler-side ``db.commit()`` releases the
      savepoint without committing the outer transaction. Teardown rolls the
      outer transaction back, undoing every write across the test. This
      isolation works whether or not the test code itself calls commit, and
      is required for cc-v2 POST handler tests (cc2-s38) that commit
      mid-request. The engine is reused across tests to avoid connection
      churn.
    """
    import gdx_dispatch.models.platform  # noqa: F401 — register mappers
    import gdx_dispatch.models.platform_extensions  # noqa: F401 — register mappers
    from gdx_dispatch.control.models import Base as ControlBase

    pg_url = os.environ.get("GDX_TEST_CONTROL_DB_URL", "").strip()
    if pg_url:
        # PG integration gate path — alembic has already populated the schema.
        # Reuse a module-level cached engine to avoid reconnecting per test.
        #
        # Isolation pattern: SAVEPOINT-wrapped session bound to a single
        # connection inside an outer BEGIN. Handler-side ``db.commit()`` (which
        # cc-v2 POST endpoints do at the end of every mutation) RELEASES the
        # savepoint rather than committing the outer txn; the
        # ``after_transaction_end`` listener immediately re-creates a savepoint
        # so the next handler can commit again, etc. Teardown rolls the OUTER
        # transaction back, undoing every staged write across the test.
        #
        # This is the canonical SQLAlchemy "joining a session to an external
        # transaction" pattern — required for any test that calls into a
        # FastAPI handler whose code path commits.
        engine = _pg_integration_engine(pg_url)
        connection = engine.connect()
        outer_tx = connection.begin()
        db = sessionmaker(bind=connection, autoflush=False, autocommit=False)()
        nested = connection.begin_nested()

        @event.listens_for(db, "after_transaction_end")
        def _restart_savepoint(session, transaction):
            nonlocal nested
            # Only re-create the savepoint when the just-ended transaction was
            # the inner SAVEPOINT (its parent is the outer txn, not another
            # savepoint). Without this guard, nested-nested savepoints inside
            # tests (e.g. test_granter_trigger_chaos) trigger an extra
            # begin_nested at the wrong level.
            if transaction.nested and not transaction._parent.nested:
                nested = connection.begin_nested()

        try:
            yield db
        finally:
            db.close()
            if outer_tx.is_active:
                outer_tx.rollback()
            connection.close()
    else:
        # Default SQLite-in-memory path — fast + isolated, per-test engine.
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        ControlBase.metadata.create_all(engine, checkfirst=True)
        db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
        try:
            yield db
        finally:
            db.close()
            engine.dispose()


_PG_ENGINE_CACHE: dict = {}


def _pg_integration_engine(url: str):
    """Return a cached SQLAlchemy engine for the PG integration gate.

    One engine per process; each test session gets its own transaction from
    the shared engine's pool.
    """
    if url not in _PG_ENGINE_CACHE:
        _PG_ENGINE_CACHE[url] = create_engine(url, future=True, pool_pre_ping=True)
    return _PG_ENGINE_CACHE[url]


# ── SS-5 Slice A platform harness fixtures ─────────────────────────────────
# Re-exported from gdx_dispatch.tests.fixtures so pytest discovery picks them up here.
from gdx_dispatch.tests.fixtures.installations import make_installation_with_key  # noqa: E402,F401
from gdx_dispatch.tests.fixtures.keypairs import test_app_keypair  # noqa: E402,F401
from gdx_dispatch.tests.fixtures.pg import (  # noqa: E402,F401
    pg_template_db,
    pg_test_db,
    pg_test_engine,
    pg_test_session,
)
from gdx_dispatch.tests.fixtures.shares import make_dual_tenant_share_setup  # noqa: E402,F401
