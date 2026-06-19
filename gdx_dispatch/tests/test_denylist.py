"""SS-7 Slice C — hermetic unit tests for :mod:`gdx_dispatch.core.denylist`.

No FastAPI, no DB fixtures, no Redis, no JWT decoding. Every test builds
its own :class:`Denylist` and passes ``now`` explicitly so expiry
behaviour is deterministic across CI machines and local clocks.

These tests pin the *pre-check* contract: the denylist answers "has this
token been revoked?" and nothing else. They deliberately do not import
:mod:`gdx_dispatch.core.policy` so a later slice that accidentally wires the
denylist into ``evaluate(...)`` will fail either the policy tests or
these tests rather than silently conflating the two surfaces.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from gdx_dispatch.core.denylist import Denylist

EPOCH = datetime(2026, 4, 17, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Add + hit path
# ---------------------------------------------------------------------------


def test_add_then_contains_returns_true_while_not_expired():
    dl = Denylist()
    dl.add("jti-hit", EPOCH + timedelta(minutes=5))

    assert dl.contains("jti-hit", now=EPOCH) is True


def test_contains_true_just_before_expiry_boundary():
    """An entry whose ``expires_at`` is one microsecond in the future is
    still revoked. Pinning this keeps the boundary semantics explicit:
    ``expires_at`` is the first instant at which the entry is no longer
    revoked, not the last instant it is."""
    dl = Denylist()
    expires = EPOCH + timedelta(microseconds=1)
    dl.add("jti-edge", expires)

    assert dl.contains("jti-edge", now=EPOCH) is True


def test_re_add_overwrites_prior_expiry():
    """Last write wins — a fresh ``add`` with a later ``expires_at``
    supersedes the earlier revocation. Matches the sliding-window
    revocation pattern where a re-issued token's ``exp`` replaces the
    prior one."""
    dl = Denylist()
    dl.add("jti-slide", EPOCH + timedelta(seconds=1))
    dl.add("jti-slide", EPOCH + timedelta(minutes=10))

    # well past the first expiry, before the second
    assert dl.contains("jti-slide", now=EPOCH + timedelta(seconds=30)) is True


# ---------------------------------------------------------------------------
# Miss path
# ---------------------------------------------------------------------------


def test_contains_false_when_jti_never_added():
    dl = Denylist()

    assert dl.contains("jti-never-added", now=EPOCH) is False


def test_contains_false_on_fresh_instance():
    """Fresh :class:`Denylist` instances must not share state with any
    other instance — guards against accidentally making ``_entries`` a
    class-level default (a classic Python footgun)."""
    dl_a = Denylist()
    dl_a.add("jti-a", EPOCH + timedelta(hours=1))

    dl_b = Denylist()
    assert dl_b.contains("jti-a", now=EPOCH) is False


# ---------------------------------------------------------------------------
# Expiry behaviour
# ---------------------------------------------------------------------------


def test_expired_entry_is_miss_and_opportunistically_pruned():
    dl = Denylist()
    dl.add("jti-stale", EPOCH + timedelta(minutes=1))

    after_expiry = EPOCH + timedelta(minutes=2)

    # First lookup after expiry: returns False AND drops the entry.
    assert dl.contains("jti-stale", now=after_expiry) is False
    # Internal state: pruning happened, so purge_expired now finds nothing.
    assert dl.purge_expired(now=after_expiry) == 0


def test_expired_entry_at_exact_boundary_is_miss():
    """When ``now == expires_at``, the entry is expired (<= comparison).
    This closes the off-by-one where a token's own ``exp`` claim would
    briefly linger as "still revoked" at the instant it naturally
    expires."""
    dl = Denylist()
    expires = EPOCH + timedelta(minutes=1)
    dl.add("jti-boundary", expires)

    assert dl.contains("jti-boundary", now=expires) is False


# ---------------------------------------------------------------------------
# purge_expired count behaviour
# ---------------------------------------------------------------------------


def test_purge_expired_drops_only_expired_entries_and_returns_count():
    dl = Denylist()
    dl.add("jti-gone-1", EPOCH + timedelta(seconds=10))
    dl.add("jti-gone-2", EPOCH + timedelta(seconds=20))
    dl.add("jti-live", EPOCH + timedelta(hours=1))

    dropped = dl.purge_expired(now=EPOCH + timedelta(minutes=1))

    assert dropped == 2
    # Live entry survives.
    assert dl.contains("jti-live", now=EPOCH + timedelta(minutes=1)) is True
    # Expired entries stay gone on a second purge.
    assert dl.purge_expired(now=EPOCH + timedelta(minutes=1)) == 0


def test_purge_expired_returns_zero_on_empty_denylist():
    dl = Denylist()

    assert dl.purge_expired(now=EPOCH) == 0


def test_purge_expired_returns_zero_when_nothing_expired():
    dl = Denylist()
    dl.add("jti-live-1", EPOCH + timedelta(hours=1))
    dl.add("jti-live-2", EPOCH + timedelta(hours=2))

    assert dl.purge_expired(now=EPOCH) == 0
    assert dl.contains("jti-live-1", now=EPOCH) is True
    assert dl.contains("jti-live-2", now=EPOCH) is True


# ---------------------------------------------------------------------------
# Blank / invalid jti defensive handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank_jti", ["", None])
def test_add_ignores_blank_jti_without_crashing(blank_jti):
    dl = Denylist()

    # Must not raise. ``None`` is included because SS-7 Slice D will
    # feed ``principal.jti`` through here, and a missing ``jti`` claim
    # surfaces as ``None`` until the validator promotes it to a string.
    dl.add(blank_jti, EPOCH + timedelta(hours=1))  # type: ignore[arg-type]

    # And must not have been stored.
    assert dl.purge_expired(now=EPOCH + timedelta(days=365)) == 0


@pytest.mark.parametrize("blank_jti", ["", None])
def test_contains_returns_false_for_blank_jti(blank_jti):
    dl = Denylist()
    dl.add("jti-real", EPOCH + timedelta(hours=1))

    assert dl.contains(blank_jti, now=EPOCH) is False  # type: ignore[arg-type]


def test_blank_jti_does_not_shadow_a_real_revocation():
    """An attacker cannot pass a blank ``jti`` and have the denylist
    match a real revocation by accident — the blank check is a short-
    circuit miss, not a wildcard."""
    dl = Denylist()
    dl.add("jti-real", EPOCH + timedelta(hours=1))

    assert dl.contains("", now=EPOCH) is False
    # Real revocation still holds.
    assert dl.contains("jti-real", now=EPOCH) is True


# ---------------------------------------------------------------------------
# now=None default path — only once, for documentation; determinism-
# sensitive assertions use explicit ``now`` above.
# ---------------------------------------------------------------------------


def test_contains_uses_wallclock_when_now_is_none():
    """Smoke test that the ``now=None`` branch resolves to wallclock
    UTC without raising. Adds a jti with a far-future expiry so the
    assertion does not race the system clock."""
    dl = Denylist()
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    dl.add("jti-future", far_future)

    assert dl.contains("jti-future") is True


def test_purge_expired_uses_wallclock_when_now_is_none():
    """Same smoke test for :meth:`purge_expired`. Seeds an already-
    expired entry so the wallclock-default branch observes it and
    returns a count of 1 without the test depending on system time."""
    dl = Denylist()
    dl.add("jti-past", datetime(2000, 1, 1, tzinfo=timezone.utc))

    assert dl.purge_expired() == 1


# ---------------------------------------------------------------------------
# SS-7 Slice J — Redis-backed adapter seam
#
# All Slice J tests are hermetic: a tiny in-process ``_FakeRedis`` stands in
# for a real Redis server so no network is touched. The fake mirrors the
# subset of the ``redis.Redis`` contract the Denylist uses — ``setex`` and
# ``get`` with ``decode_responses=True`` semantics (``get`` returns ``str``
# or ``None``). Failure injection is done via ``_BrokenRedis`` / a single-
# method override; the adapter catches ``Exception`` broadly by design so
# both behave identically to the production fail-open path.
# ---------------------------------------------------------------------------


class _FakeRedis:
    """In-memory stand-in for the subset of the ``redis.Redis`` surface the
    Slice J adapter touches.

    ``setex(key, seconds, value)`` stores the value verbatim; ``get(key)``
    returns the stored value or ``None``. TTL is not enforced — tests that
    care about expiry assert on the Denylist's own ``now`` parameter and
    never rely on Redis' clock, so a dict is enough.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.setex_calls: list[tuple[str, int, str]] = []
        self.get_calls: list[str] = []

    def setex(self, key: str, seconds: int, value: str) -> None:
        self.setex_calls.append((key, seconds, value))
        self.store[key] = value

    def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self.store.get(key)


class _BrokenRedis:
    """Every call raises, simulating a Redis network / server outage."""

    def setex(self, *_args, **_kwargs) -> None:
        raise RuntimeError("redis setex failure (simulated)")

    def get(self, *_args, **_kwargs) -> str | None:
        raise RuntimeError("redis get failure (simulated)")


def test_no_redis_mode_preserves_local_only_behavior():
    # Regression guard for the Slice C contract: when ``redis_client`` is
    # ``None`` the Denylist must behave byte-for-byte like the Slice C /
    # Slice H local-only class. This test exists so a future refactor that
    # accidentally makes Redis mandatory fails loudly.
    dl = Denylist()
    assert dl.redis_client is None
    dl.add("jti-local", EPOCH + timedelta(hours=1))
    assert dl.contains("jti-local", now=EPOCH) is True
    # Purge still local-only.
    assert dl.purge_expired(now=EPOCH - timedelta(seconds=1)) == 0


def test_add_fans_out_to_redis_with_expected_key_ttl_and_value():
    # Slice J contract: ``add`` writes local first, then Redis. The Redis
    # key is namespaced under ``ss7:denylist:`` so the denylist can coexist
    # with other Redis users (login sessions, rate-limit counters). TTL is
    # computed from ``expires_at - now``, rounded to seconds, so Redis's
    # own TTL naturally evicts the entry at the same instant the local map
    # treats it as expired.
    fake = _FakeRedis()
    dl = Denylist(redis_client=fake)
    expires = datetime.now(timezone.utc) + timedelta(seconds=900)

    dl.add("jti-fanout", expires)

    assert len(fake.setex_calls) == 1
    key, ttl, value = fake.setex_calls[0]
    assert key == "ss7:denylist:jti-fanout"
    # TTL is ``int(total_seconds())`` so it floors — allow a one-second
    # slack for wall-clock jitter between ``add`` and the assertion.
    assert 898 <= ttl <= 900
    # Value is the ISO expiry so any worker can parse it back.
    assert value == expires.isoformat()
    # Local write still happened (proved by the fast-path hit).
    assert dl.contains("jti-fanout") is True


def test_add_skips_redis_when_expires_at_already_in_past():
    # Defensive: a caller handing in an ``expires_at`` that is already in
    # the past should not trigger a Redis SETEX with a non-positive TTL
    # (Redis raises ``ResponseError`` for that). The local write is still
    # attempted but the Denylist's ``contains`` expiry semantics prune it
    # on the next lookup, so we only assert Redis was not touched.
    fake = _FakeRedis()
    dl = Denylist(redis_client=fake)

    dl.add("jti-stale", datetime.now(timezone.utc) - timedelta(seconds=5))

    assert fake.setex_calls == []


def test_add_ignores_blank_jti_with_redis_attached():
    # Blank-jti guard still short-circuits before touching Redis — neither
    # the local map nor Redis should see the empty key. Mirrors the
    # Slice C parametrized ``test_add_ignores_blank_jti_without_crashing``.
    fake = _FakeRedis()
    dl = Denylist(redis_client=fake)

    dl.add("", EPOCH + timedelta(hours=1))

    assert fake.setex_calls == []
    assert dl.purge_expired(now=EPOCH + timedelta(days=365)) == 0


def test_contains_reads_from_redis_on_local_miss_and_hydrates_local():
    # Slice J cross-instance read: a fresh Denylist whose local map has
    # never seen ``jti-x`` must consult Redis and honor the revocation.
    # After the hit the local map is hydrated so the next ``contains``
    # is a local fast-path lookup (no additional Redis GET).
    fake = _FakeRedis()
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    fake.store["ss7:denylist:jti-x"] = expires.isoformat()

    dl = Denylist(redis_client=fake)

    # Local map is empty — Slice C contract guarantees this.
    assert "jti-x" not in dl._entries
    assert dl.contains("jti-x") is True
    assert len(fake.get_calls) == 1
    # Hydrated: second call answers from local without another Redis GET.
    assert dl.contains("jti-x") is True
    assert len(fake.get_calls) == 1


def test_contains_returns_false_when_redis_key_absent():
    # Negative path: empty Redis is a clean miss, not a false positive.
    fake = _FakeRedis()
    dl = Denylist(redis_client=fake)

    assert dl.contains("jti-never") is False
    # Redis was queried exactly once — we do not retry on a miss.
    assert fake.get_calls == ["ss7:denylist:jti-never"]


def test_contains_returns_false_when_redis_value_is_already_expired():
    # An orphaned Redis entry (TTL eviction is not instantaneous) that is
    # already past its ISO expiry must be treated as a miss. The local
    # map is NOT hydrated with a stale expiry.
    fake = _FakeRedis()
    stale_expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    fake.store["ss7:denylist:jti-stale"] = stale_expires.isoformat()

    dl = Denylist(redis_client=fake)

    assert dl.contains("jti-stale") is False
    assert "jti-stale" not in dl._entries


def test_contains_returns_false_on_unparseable_redis_value():
    # Defensive: another service dropping a non-ISO value under the same
    # key namespace must not crash auth. The adapter treats garbage as a
    # miss and keeps going.
    fake = _FakeRedis()
    fake.store["ss7:denylist:jti-garbage"] = "not-an-iso-datetime"
    dl = Denylist(redis_client=fake)

    assert dl.contains("jti-garbage") is False


def test_cross_instance_visibility_via_shared_fake_redis():
    # End-to-end Slice J property: two independent Denylist instances
    # (simulating two worker processes or two ``app.state`` namespaces)
    # share one Redis. A revoke on A is observed by B even though B's
    # local map was never written to. This is the property that buys
    # cross-worker revocation consistency.
    shared = _FakeRedis()
    dl_a = Denylist(redis_client=shared)
    dl_b = Denylist(redis_client=shared)

    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    dl_a.add("jti-shared", expires)

    # A sees it locally (fast path).
    assert dl_a.contains("jti-shared") is True
    # B's local map has no entry, so it must go to Redis.
    assert "jti-shared" not in dl_b._entries
    assert dl_b.contains("jti-shared") is True
    # B hydrated from Redis exactly once and is now on the fast path.
    assert shared.get_calls.count("ss7:denylist:jti-shared") == 1


def test_redis_write_failure_still_keeps_local_revocation_effective():
    # Fail-open write contract: Redis SETEX raising must not propagate
    # out of ``add``, and the local map must still reflect the revocation
    # so the writing worker continues to enforce it.
    broken = _BrokenRedis()
    dl = Denylist(redis_client=broken)
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)

    # Must not raise.
    dl.add("jti-localonly", expires)

    # Local observation is unaffected by Redis outage.
    assert dl.contains("jti-localonly") is True


def test_redis_read_failure_returns_miss_without_raising():
    # Fail-open read contract: a Redis GET that raises must surface as
    # ``False`` (miss) rather than an exception — a Redis outage cannot
    # be allowed to lock valid users out of the app.
    broken = _BrokenRedis()
    dl = Denylist(redis_client=broken)

    # Must not raise. Must return False.
    assert dl.contains("jti-anything") is False


def test_redis_read_failure_does_not_shadow_local_hit():
    # Mixed state: local hit + broken Redis. The local fast-path must win
    # without ever touching Redis (so the broken client is irrelevant).
    broken = _BrokenRedis()
    dl = Denylist(redis_client=broken)
    dl._entries["jti-localhit"] = datetime.now(timezone.utc) + timedelta(minutes=5)

    # ``contains`` hits local map before Redis — so even the broken
    # client's ``get`` is never called.
    assert dl.contains("jti-localhit") is True


def test_expired_local_entry_does_not_consult_redis():
    # When the local map has the jti but it's expired, the method returns
    # False WITHOUT consulting Redis — a local observation of expiry is
    # authoritative (Redis' TTL matches the same wall-clock boundary).
    fake = _FakeRedis()
    # Pre-populate Redis with a longer-lived entry to prove we skip it.
    fake.store["ss7:denylist:jti-local-exp"] = (
        EPOCH + timedelta(hours=5)
    ).isoformat()

    dl = Denylist(redis_client=fake)
    dl._entries["jti-local-exp"] = EPOCH + timedelta(minutes=1)

    # now is past the local expiry — method prunes and returns False and
    # MUST NOT fall through to Redis (which would otherwise report True).
    assert dl.contains("jti-local-exp", now=EPOCH + timedelta(minutes=2)) is False
    assert fake.get_calls == []


# ---------------------------------------------------------------------------
# SS-7 Slice K — regression guard that mode parsing stays at the router seam
#
# Slice K added a ``DENYLIST_BACKEND_MODE`` env var parsed inside
# :func:`gdx_dispatch.routers.auth._denylist_redis_client`. The pinned design
# decision is that this env var is NOT read by the :class:`Denylist` core
# class — keeping the adapter-agnostic core out of the configuration
# surface. A future refactor that leaks the mode env into this module
# would fail the test below.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode_value", ["memory", "redis", "garbage"])
def test_denylist_core_ignores_backend_mode_env(monkeypatch, mode_value):
    # Set the Slice K env var to each meaningful value (memory / redis /
    # invalid) and verify the :class:`Denylist` core class behaves
    # identically to the unset-env baseline. The mode is parsed at the
    # router seam (``gdx_dispatch.routers.auth._denylist_redis_client``) and must
    # not leak into this module — an accidental ``os.getenv`` call inside
    # ``Denylist.add`` / ``contains`` / ``purge_expired`` would surface as
    # behavior change here.
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", mode_value)
    # Baseline assumption: env var exists only at the router seam; the
    # core class should never need to look at it.
    assert os.getenv("DENYLIST_BACKEND_MODE") == mode_value

    dl = Denylist()
    # Default constructor yields local-only regardless of env.
    assert dl.redis_client is None

    dl.add("jti-mode-independence", EPOCH + timedelta(hours=1))

    # Core add/contains/purge behavior is identical to the Slice C
    # baseline across every mode value. A regression that makes
    # ``Denylist`` read the env would make one of these branch on mode.
    assert dl.contains("jti-mode-independence", now=EPOCH) is True
    assert dl.contains("jti-never-added", now=EPOCH) is False
    # purge_expired with a now before expiry is still a no-op.
    assert dl.purge_expired(now=EPOCH) == 0
