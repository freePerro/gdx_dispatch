"""Guard for the autouse per-test Redis isolation fixture (GDXA-109).

The fixture in ``conftest.py`` (``_isolated_redis``) is what stops a test's
Redis writes from leaking into a real server, where a constant tenant id and
a 30 s TTL turned them into state shared across tests, across pytest
processes and across previous runs. It is a few lines of monkeypatching with
no natural failure mode, so without these tests any of the following would
silently restore the defect and every suite would stay green:

* dropping the ``("gdx_dispatch.core.cache", "get_redis_client")`` target as
  "redundant" — it is not, because ``cache.py`` from-imports the name and so
  keeps its own binding, and that binding is the one every ``cached()`` call
  site actually uses;
* handing every test the *same* fake instead of one per test;
* replacing the fake with a stub that never round-trips, which is how the
  cache path came to be untested in the first place.

Each test names the input that turns it red, per CLAUDE.md: a green ratchet
proves nothing unless it can fail for the defect it guards. Verified by
mutation — deleting the fixture's single patch target reddens three of these
plus ``test_list_customers_excludes_soft_deleted``, the original symptom.

That mutation check only reddens **with a Redis reachable** (it was run
against a throwaway ``redis:7-alpine``). With none, the unfixed code is green
too — which is precisely why the defect read as a flake for so long, and why
these tests matter: they hold in both environments, so they do not depend on
the one condition that hid the bug.
"""

from __future__ import annotations

import pytest

from gdx_dispatch.core import cache, rate_limiter

pytestmark = pytest.mark.anyio

_LEAK_KEY = "cache:gdxa109-leak-probe"


def _is_fake(client: object) -> bool:
    return type(client).__module__.startswith("fakeredis")


# ── the client the cache resolves must be the fake ─────────────────────────
# Red when: the fixture is removed or retargeted, so cache.py falls back to
# redis://localhost:6379/0 — a real, shared server.

async def test_cache_module_resolves_the_fake_not_a_real_client():
    """``cache.py`` from-imports ``get_redis_client``, so it holds its own
    binding — and that binding is the one every ``cached()`` call site
    resolves. Retargeting the fixture at ``rate_limiter``'s copy instead
    would leave all three call sites on the real client."""
    assert _is_fake(cache.get_redis_client()), (
        "gdx_dispatch.core.cache.get_redis_client() returned a real Redis client — "
        "the customers list, branding_public and drive_time caches are leaking "
        "to a shared server again"
    )


async def test_the_rate_limiter_is_deliberately_left_on_the_real_client():
    """Scope boundary, not an oversight.

    ``RateLimiter`` shares ``rate_limiter.get_redis_client`` with the cache,
    but handing it a working Redis switches the limiter ON for every test.
    It currently fails open when Redis is unreachable, and seven
    ``test_role_permissions`` tests depend on that — with a live fake they
    429. Isolating it is real work that has to land with those tests
    rewritten, so it is filed, not bundled (GDXA-109).

    If you are here because you just extended the fixture to cover the rate
    limiter: good — update those tests in the same change, then delete this
    one.
    """
    assert not _is_fake(rate_limiter.get_redis_client()), (
        "the fixture now isolates the rate limiter too — re-check the "
        "test_role_permissions rate-limit tests, which were written against a "
        "limiter that fails open, then remove this test"
    )


# ── isolation: the pair below is the actual cross-test regression net ──────
# These two run in file order. The first writes a key the way a cached()
# endpoint does; the second asserts it is gone. Red when: the fixture is made
# session-scoped, or hands out one shared fake, or is removed entirely while
# a real Redis is reachable — which is exactly the GDXA-109 failure.

async def test_a_write_that_would_leak_into_the_next_test():
    await cache.get_redis_client().setex(_LEAK_KEY, 30, "written by the previous test")
    assert await cache.get_redis_client().get(_LEAK_KEY) == "written by the previous test"


async def test_the_previous_tests_write_is_not_visible_here():
    leaked = await cache.get_redis_client().get(_LEAK_KEY)
    assert leaked is None, (
        f"{_LEAK_KEY!r} survived from the previous test ({leaked!r}) — per-test "
        "Redis isolation is gone, so cache state crosses tests again"
    )


# ── the coverage the fake buys: the cache round-trip now actually runs ─────
# With no Redis reachable, cached() swallowed its own connection errors and
# degraded to calling the fetcher every time, so nothing exercised the read,
# the write, or the family invalidation. These are the first tests to.

async def test_cached_serves_the_second_call_from_the_cache():
    """Red when: the write leg of ``cached()`` stops persisting or the read
    leg stops being consulted — either way the fetcher runs twice."""
    calls: list[int] = []

    async def _fetch():
        calls.append(1)
        return {"items": ["from the db"]}

    first = await cache.cached("t-109", "probe:page=1", ttl_seconds=30, fetcher=_fetch)
    second = await cache.cached("t-109", "probe:page=1", ttl_seconds=30, fetcher=_fetch)

    assert first == {"items": ["from the db"]}
    assert second == first, "second call did not return the cached value"
    assert len(calls) == 1, f"fetcher ran {len(calls)} times — the cache did not hit"


async def test_invalidate_prefix_clears_the_whole_key_family():
    """The customers list fans its cache out over q/page/per_page and clears
    the family with ``invalidate_prefix``. Red when: the scan pattern or the
    delete loop breaks and a mutation leaves stale pages being served."""
    async def _fetch_page1():
        return {"page": 1}

    async def _fetch_page2():
        return {"page": 2}

    await cache.cached("t-109", "customers:q=:page=1:per=50", 30, _fetch_page1)
    await cache.cached("t-109", "customers:q=:page=2:per=50", 30, _fetch_page2)

    redis = cache.get_redis_client()
    assert await redis.get("cache:t-109:customers:q=:page=1:per=50") is not None
    assert await redis.get("cache:t-109:customers:q=:page=2:per=50") is not None

    await cache.invalidate_prefix("t-109", "customers:q=:")

    assert await redis.get("cache:t-109:customers:q=:page=1:per=50") is None, (
        "invalidate_prefix left page=1 cached — a mutation would serve a stale list"
    )
    assert await redis.get("cache:t-109:customers:q=:page=2:per=50") is None, (
        "invalidate_prefix left page=2 cached — the family clear misses the fan-out"
    )


async def test_a_test_can_still_force_the_redis_down_degrade_path(monkeypatch):
    """The fixture must not take away a test's ability to exercise the
    outage path. Red when: the fixture patches something a test body cannot
    override, so degrade-on-Redis-failure becomes untestable."""
    class _Broken:
        async def get(self, *_a, **_k):
            raise ConnectionError("redis down")

        async def setex(self, *_a, **_k):
            raise ConnectionError("redis down")

    calls: list[int] = []

    async def _fetch():
        calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(cache, "get_redis_client", lambda: _Broken())

    first = await cache.cached("t-109", "degrade:probe", ttl_seconds=30, fetcher=_fetch)
    second = await cache.cached("t-109", "degrade:probe", ttl_seconds=30, fetcher=_fetch)

    assert first == {"ok": True} and second == {"ok": True}
    assert len(calls) == 2, "with Redis down the fetcher must run every time"
