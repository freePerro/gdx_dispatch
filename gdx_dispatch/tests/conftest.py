"""
GDX test configuration.
Each test file gets isolated SQLite in-memory databases — no shared state.
"""
import os
import sqlite3
from datetime import UTC, date, datetime
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

# Register ALL tenant/control mappers up front. Several tests (PII
# encryption attestation, raw-SQL scans, model-inventory checks) assert on
# the live mapper registry; before this import they only passed when an
# earlier-collected test file happened to import the models — i.e. their
# outcome depended on pytest-split shard boundaries. Class-level fix for
# the 2026-08-04 unforked-suite find; individual files also import it so
# they stay standalone-runnable.
import gdx_dispatch.models  # noqa: E402,F401


def iter_app_routes(app):
    """Yield ``(full_path, route)`` for every leaf route in ``app``.

    FastAPI >=0.137 stopped flattening ``include_router()`` into ``app.routes``:
    each include now inserts a lazy ``_IncludedRouter`` wrapper (no ``.path``)
    instead of the sub-router's concrete routes. Route-registration tests that
    did ``{r.path for r in app.routes}`` therefore saw only ~14 top-level routes
    and raised ``AttributeError`` on the wrappers. This recurses the wrappers
    (re-applying each include prefix) and falls back to flat iteration on older
    FastAPI where ``_IncludedRouter`` doesn't exist.

    Coupling note: this reaches three private FastAPI internals
    (``_IncludedRouter``, ``.include_context.prefix``, ``.original_router``).
    ``requirements.txt`` floats ``fastapi>=0.135.3,<1.0``, so a future patch that
    renames any of them breaks every route-introspection test at once. That's a
    LOUD failure (red CI on the route tests), not a silent one — when it fires,
    update this helper (or pin fastapi), don't paper over it.
    """
    try:
        from fastapi.routing import _IncludedRouter
    except ImportError:  # older FastAPI — routes are already flat
        _IncludedRouter = ()

    def _walk(routes, prefix=""):
        for rt in routes:
            if _IncludedRouter and isinstance(rt, _IncludedRouter):
                sub = prefix + getattr(rt.include_context, "prefix", "")
                yield from _walk(rt.original_router.routes, sub)
            elif hasattr(rt, "path"):
                yield prefix + rt.path, rt

    yield from _walk(app.routes)


def app_route_paths(app):
    """Set of every registered full path (see :func:`iter_app_routes`)."""
    return {path for path, _ in iter_app_routes(app)}


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

    # Push notifications
    try:
        from gdx_dispatch.core.push_notifications import _subscriptions
        _subscriptions.clear()
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
# Cross-test isolation: give every test its own Redis for the cache path
# ---------------------------------------------------------------------------
# ``core/cache.py`` resolves its client from ``REDIS_URL`` and falls back to
# ``redis://localhost:6379/0`` — a REAL server — while the keys it builds are
# constants under test, because ``company_id()`` reads the ``GDX_TENANT_ID``
# pinned above. So wherever a Redis happens to be reachable, every test that
# hits a ``cached()`` endpoint reads and writes the SAME key as every other
# test, every parallel pytest process, and every previous run. Per-test DB
# isolation cannot touch that: the shared state is not in the process.
#
# The customers list is the instance that bit (GDXA-109). ``list_customers``
# caches ``cache:<GDX_TENANT_ID>:customers:q=:page=1:per=50`` for 30 s, so
# ``test_list_customers_excludes_soft_deleted`` served the empty result a
# previous run had written and ``test_list_customers_empty`` served that
# run's leftover row. With a Redis up the file was deterministically red
# (1 failed / 28 passed, three runs identical); with none, green. It read as
# a flake for two reasons: ``cached()`` swallows its own errors, so a degrade
# and a hit look identical from outside, and a HIT does not re-write, so a
# stale entry keeps its original residual TTL — the run landing inside the
# 30 s window failed and the next ones fell past expiry and passed.
#
# So the fixture's job is to make a test's result independent of whether a
# Redis is reachable. Each test gets its own ``FakeServer``: an isolated
# in-process keyspace, nothing shared, nothing external. Guarded by
# ``test_redis_isolation_fixture.py``.
#
# A fake rather than stubbing the cache out, because with no Redis the
# ``cached()`` read/write path degraded to a no-op — nothing proved the 30 s
# cache or its ``invalidate_prefix()`` family-clear worked at all. The fake
# exercises the real round-trip. A test that wants the degrade path still
# gets it: patching this same attribute inside the test body wins.
#
# Scope is deliberately just the cache. ``cache.py`` does ``from ...
# rate_limiter import get_redis_client``, so it holds its OWN module binding,
# and that binding is what all three ``cached()`` call sites (customers list,
# branding_public, drive_time) resolve — patching it closes the whole cache
# leak while leaving ``rate_limiter.get_redis_client`` alone. That last part
# is load-bearing, not incidental: the rate limiter shares that factory, it
# currently fails OPEN under test (its long-lived client is bound to an
# earlier test's event loop, so calls raise and the limiter allows), and
# ``_privileged_write_rate_limit`` is 1 write/sec/actor. Give it a working
# client and test_role_permissions goes 14 failures -> 20 under a live Redis.
# Measured both ways; do not "finish the job" here without rewriting those
# tests in the same change.
#
# The other helpers defaulting to a real localhost:6379 — rate_limiter,
# circuit_breaker, terminology, api_keys, onboarding, auth_revoke,
# bank_feeds/oauth — are the same SHAPE and still carry the same latent
# cross-run exposure. Isolating them changes what those subsystems DO under
# test, not just where their state lives, so each is separate work with its
# own tests. Reported on GDXA-109 rather than bundled into a flake fix.
#
# One more, listed because it is the awkward case: routers/auth/core.py:176
# binds ``redis = from_url(...)`` at MODULE scope and admin_ops.py imports
# that OBJECT (as ``_auth_redis``), not a factory — so a factory-patching
# fixture like this one structurally cannot reach it. It keys on
# ``pw_reset:<random token>``, so it cannot collide the way a constant key
# does; it is the shape without the flake.


@pytest.fixture(autouse=True, scope="function")
def _isolated_redis(monkeypatch):
    """Point ``cached()`` at a per-test in-process Redis."""
    import fakeredis.aioredis

    from gdx_dispatch.core import cache

    client = fakeredis.aioredis.FakeRedis(
        server=fakeredis.FakeServer(), decode_responses=True
    )
    # Only cache.py's binding. NOT a cache_clear() on the real lru_cache:
    # that object is shared with the rate limiter (see above), and clearing
    # it hands the limiter a fresh, correctly loop-bound client.
    monkeypatch.setattr(cache, "get_redis_client", lambda: client)
    yield


# ---------------------------------------------------------------------------
# Shop-midday clock: for tests whose fixtures are built from `now` while the
# code under test compares SHOP days (GDXA-361)
# ---------------------------------------------------------------------------
# The phone's job timer only counts if its clock_in is on now's shop day
# (`_todays_open_job_timer`, plan §5.4a B8), and with no AppSettings row the
# shop day is America/New_York's. A test that clocks in at `now - 187 min` was
# therefore on YESTERDAY's shop day for the first hours after New York
# midnight, 04:00-08:00 UTC in summer and 05:00-09:00 in winter, and Stop
# answered 404. The day-close harness's `at(0)` (00:05 shop time) is in the
# future for the first five minutes of the shop day. Every PR's CI went red in
# those windows (GDXA-359). Same class as #677, which pinned UTC midday for a
# UTC-midnight clamp; this pins SHOP midday for a shop-day rule.
#
# 13:00 New York leaves room on both sides: an offset under 13 hours stays
# today, one over 13 hours lands on yesterday (the labor-trail file's 20 h and
# 30 h stale timers). Each opting-in file asserts
# its own widest offsets against this instant.
#
# `tick=True` so the clock still moves: rows written one after another keep
# distinct, ordered clock_ins, which `ORDER BY clock_in DESC` readers rely on.
SHOP_MIDDAY_UTC = datetime(2026, 1, 15, 18, 0, 0, tzinfo=UTC)  # 13:00 EST
SHOP_MIDDAY_TZ = "America/New_York"  # shop_tz's fallback with no AppSettings row


@pytest.fixture
def shop_midday_clock():
    """Pin `now` to SHOP_MIDDAY_UTC (ticking). Opt in per module with
    ``pytestmark = pytest.mark.usefixtures("shop_midday_clock")``."""
    import freezegun

    # As in test_timeclock_status_breaks: keep pytest's own timers real, or
    # --durations reports a frozen test as ~56 years. Global and idempotent.
    freezegun.configure(extend_ignore_list=["_pytest"])
    with freezegun.freeze_time(SHOP_MIDDAY_UTC, tick=True):
        yield


# ---------------------------------------------------------------------------
# Fresh DB factory
# ---------------------------------------------------------------------------

def make_fresh_db():
    """Create a fully isolated in-memory SQLite DB with all tenant tables."""
    # gdx_dispatch.models is imported at conftest top level (registers all mappers).
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

    # Next-action queue table
    try:
        from gdx_dispatch.core.next_action import NextAction
        NextAction.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass

    return engine


def production_sessionmaker(bind):
    """A sessionmaker on ``bind`` configured exactly as ``core.database.SessionLocal``.

    Copied from ``SessionLocal.kw`` rather than restated, so a test session
    cannot drift from production (GDXA-368). The point is ``expire_on_commit``:
    production leaves it True, so reading an attribute off a just-committed
    instance issues a SELECT — and 500s if that read happens after the session
    is closed or its connection is gone. A factory pinned to
    ``expire_on_commit`` off serves that read from memory and can never show
    the bug. ``test_no_expire_on_commit_pin.py`` keeps the pin
    out of the suite.
    """
    from gdx_dispatch.core.database import SessionLocal as _prod

    kw = {k: v for k, v in _prod.kw.items() if k != "bind"}
    return sessionmaker(bind=bind, **kw)


@pytest.fixture
def tenant_db():
    """Isolated tenant DB for test_02 e2e tests."""
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def dead_after_commit():
    """Kill an engine the instant one of its COMMITs succeeds (GDXA-145).

    Use as ``with dead_after_commit(engine) as seam:`` — inside the block, every
    SQL statement issued on ``engine`` after its first successful commit raises,
    the way psycopg does when pgbouncer restarts or PG fails over between a
    handler's COMMIT and its next statement. ``seam["refused"]`` counts what it
    stopped, so a test can assert a handler issued *zero* statements after
    committing rather than only that it survived one failing.

    Why an engine event and not a patched ``Session.refresh``: the window has to
    close on ANY post-commit statement, and the dangerous one is often a
    statement nobody wrote — ``SessionLocal`` leaves ``expire_on_commit`` at its
    default True, so reading one column off a just-committed ORM instance
    issues a SELECT.

    ``fail_rollback=True`` additionally fails the FIRST DBAPI rollback after the
    commit, which a refusing cursor does NOT cover: SQLAlchemy rolls back through
    ``dialect.do_rollback``, not through a cursor, so a `db.rollback()` inside a
    post-commit `except` block succeeds under the default seam and raises on a
    genuinely dead connection. That is the shape that escapes a best-effort
    block and answers a bare 500 for a committed row.

    Only the first two, deliberately, and the number is load-bearing in both
    directions:

    * Not fewer. A refused statement makes SQLAlchemy roll back on its own error
      path before the handler's `except` runs, so the handler's own
      `db.rollback()` is the *second* — capping at one lets it succeed and the
      seam sees nothing (measured: the bare-rollback variant went green).
    * Not more. A failed reset-on-return makes the pool invalidate the
      connection, and for a ``sqlite://`` StaticPool fixture that discards the
      database itself — measured: the next test in the module died on "no such
      table: webhook_endpoints". Letting the teardown rollback succeed is what
      keeps this seam non-destructive on a shared in-memory engine, and it is
      also why what a failing *teardown* rollback does cannot be pinned here.

    Warm the path before arming. A handler whose first statement is
    ``ensure_audit_table(db)`` commits there on that table's first run per
    engine, and that commit would arm the seam before the write under test —
    send one successful request through the same route first.
    """
    import contextlib as _contextlib

    from sqlalchemy import event as _event

    _ROLLBACK_FAILURES = 2  # see the docstring — both bounds are measured

    @_contextlib.contextmanager
    def _seam(engine, *, fail_rollback=False):
        state = {"armed": False, "refused": 0, "rollbacks_failed": 0}

        def _arm(conn):  # ConnectionEvents.commit
            state["armed"] = True

        def _refuse(conn, cursor, statement, parameters, context, executemany):
            if state["armed"]:
                state["refused"] += 1
                raise RuntimeError("server closed the connection unexpectedly")

        dialect = engine.dialect
        real_rollback = dialect.do_rollback

        def _refuse_rollback(dbapi_connection):
            if state["armed"] and state["rollbacks_failed"] < _ROLLBACK_FAILURES:
                state["rollbacks_failed"] += 1
                raise RuntimeError("server closed the connection unexpectedly")
            return real_rollback(dbapi_connection)

        _event.listen(engine, "commit", _arm)
        _event.listen(engine, "before_cursor_execute", _refuse)
        if fail_rollback:
            dialect.do_rollback = _refuse_rollback
        try:
            yield state
        finally:
            state["armed"] = False
            if fail_rollback:
                dialect.do_rollback = real_rollback
            _event.remove(engine, "commit", _arm)
            _event.remove(engine, "before_cursor_execute", _refuse)

    return _seam


@pytest.fixture
def control_db():
    """Isolated control plane DB (tenants / tenant_settings / games).

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
    from gdx_dispatch.core.tenant_settings import Base as ControlBase

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


# Re-exported from gdx_dispatch.tests.fixtures so pytest discovery picks them up here.
from gdx_dispatch.tests.fixtures.keypairs import test_app_keypair  # noqa: E402,F401
from gdx_dispatch.tests.fixtures.pg import (  # noqa: E402,F401
    pg_template_db,
    pg_test_db,
    pg_test_engine,
    pg_test_session,
)
