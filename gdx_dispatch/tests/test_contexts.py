"""SS-8 Slice B — unit tests for ``gdx_dispatch.core.contexts`` execution-context shells.

Storage-only contract: the two ``ContextVar`` instances expose default
reads, token-based set/reset semantics, and per-task isolation under
``asyncio``. No FastAPI, no DB, no JWT — those land in later SS-8 slices.

Each test is a deposition against the contract:

* defaults are ``None`` and the immutable empty tuple ``()``
* ``.set(...)`` returns a token that ``.reset(token)`` uses to restore
  the prior value (including the original default)
* nested ``set``/``reset`` correctly unwinds outer values
* sibling asyncio tasks see independent context views (no cross-leak)
"""
from __future__ import annotations

import asyncio

import pytest

from gdx_dispatch.core.contexts import (
    async_execution_context,
    current_act_chain,
    current_installation_id,
    execution_context,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_current_installation_id_default_is_none():
    assert current_installation_id.get() is None


def test_current_act_chain_default_is_empty_tuple():
    value = current_act_chain.get()
    assert value == ()
    assert isinstance(value, tuple)


# ---------------------------------------------------------------------------
# Set / reset token semantics
# ---------------------------------------------------------------------------


def test_installation_id_set_then_reset_restores_default():
    assert current_installation_id.get() is None

    token = current_installation_id.set("inst-abc")
    try:
        assert current_installation_id.get() == "inst-abc"
    finally:
        current_installation_id.reset(token)

    assert current_installation_id.get() is None


def test_act_chain_set_then_reset_restores_default():
    assert current_act_chain.get() == ()

    token = current_act_chain.set(("user-1", "app-1"))
    try:
        assert current_act_chain.get() == ("user-1", "app-1")
    finally:
        current_act_chain.reset(token)

    assert current_act_chain.get() == ()


def test_nested_installation_id_set_reset_unwinds_outer_value():
    assert current_installation_id.get() is None

    outer_token = current_installation_id.set("inst-outer")
    try:
        assert current_installation_id.get() == "inst-outer"

        inner_token = current_installation_id.set("inst-inner")
        try:
            assert current_installation_id.get() == "inst-inner"
        finally:
            current_installation_id.reset(inner_token)

        # Resetting the inner token must restore the OUTER value, not the
        # original default — this is the contract that guards against
        # contextvar-stack misuse.
        assert current_installation_id.get() == "inst-outer"
    finally:
        current_installation_id.reset(outer_token)

    assert current_installation_id.get() is None


def test_nested_act_chain_set_reset_unwinds_outer_value():
    assert current_act_chain.get() == ()

    outer_token = current_act_chain.set(("user-outer",))
    try:
        assert current_act_chain.get() == ("user-outer",)

        inner_token = current_act_chain.set(("user-inner", "app-inner"))
        try:
            assert current_act_chain.get() == ("user-inner", "app-inner")
        finally:
            current_act_chain.reset(inner_token)

        assert current_act_chain.get() == ("user-outer",)
    finally:
        current_act_chain.reset(outer_token)

    assert current_act_chain.get() == ()


# ---------------------------------------------------------------------------
# Asyncio sibling-task isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_installation_id_isolated_between_sibling_asyncio_tasks():
    """Setting the installation_id in one task must not leak into a sibling.

    ``asyncio.create_task`` copies the current context at task creation, so
    each task has its own ContextVar slot. We start both tasks from the
    SAME parent context (default ``None``), have one task ``.set(...)`` a
    value, and assert the sibling still observes the default.
    """
    setter_observed: list[str | None] = []
    sibling_observed: list[str | None] = []
    sibling_started = asyncio.Event()
    setter_done = asyncio.Event()

    async def setter():
        # Wait until the sibling has started so the assertion is genuinely
        # concurrent (not just serialized after our reset).
        await sibling_started.wait()
        token = current_installation_id.set("inst-setter")
        try:
            setter_observed.append(current_installation_id.get())
            setter_done.set()
            # Hold the value briefly while sibling re-checks its own slot.
            await asyncio.sleep(0)
        finally:
            current_installation_id.reset(token)

    async def sibling():
        sibling_started.set()
        # Take an initial reading concurrently with setter's set().
        sibling_observed.append(current_installation_id.get())
        await setter_done.wait()
        # Re-read AFTER the setter has set its own slot — sibling must
        # still see the default because contexts are per-task.
        sibling_observed.append(current_installation_id.get())

    assert current_installation_id.get() is None
    await asyncio.gather(setter(), sibling())

    assert setter_observed == ["inst-setter"]
    assert sibling_observed == [None, None]
    # Parent context unaffected by either task.
    assert current_installation_id.get() is None


@pytest.mark.asyncio
async def test_act_chain_isolated_between_sibling_asyncio_tasks():
    setter_observed: list[tuple[str, ...]] = []
    sibling_observed: list[tuple[str, ...]] = []
    sibling_started = asyncio.Event()
    setter_done = asyncio.Event()

    async def setter():
        await sibling_started.wait()
        token = current_act_chain.set(("user-x", "app-y"))
        try:
            setter_observed.append(current_act_chain.get())
            setter_done.set()
            await asyncio.sleep(0)
        finally:
            current_act_chain.reset(token)

    async def sibling():
        sibling_started.set()
        sibling_observed.append(current_act_chain.get())
        await setter_done.wait()
        sibling_observed.append(current_act_chain.get())

    assert current_act_chain.get() == ()
    await asyncio.gather(setter(), sibling())

    assert setter_observed == [("user-x", "app-y")]
    assert sibling_observed == [(), ()]
    assert current_act_chain.get() == ()


# ---------------------------------------------------------------------------
# SS-8 Slice D — scoped ``execution_context`` helper
# ---------------------------------------------------------------------------
#
# Slice D adds a single-site bundled-set helper so no future caller has to
# hand-write the matching ``.reset(token)`` for both contextvars. The tests
# below prove:
#   * both vars are bound inside the with-block and restored to defaults
#     afterward;
#   * exceptions raised inside the block still trigger both resets;
#   * nested scopes unwind to the ENCLOSING values (not the defaults);
#   * pre-existing non-default state is preserved across the block.


def test_execution_context_sets_both_and_restores_on_exit():
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    with execution_context(
        installation_id="inst-bundle",
        act_chain=("user-a", "app-b"),
    ):
        assert current_installation_id.get() == "inst-bundle"
        assert current_act_chain.get() == ("user-a", "app-b")
        # act_chain must remain an immutable tuple — Slice A contract.
        assert isinstance(current_act_chain.get(), tuple)

    # Both vars restored to defaults after the with-block exits cleanly.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()


def test_execution_context_restores_both_when_body_raises():
    # Exception-safety is the whole reason the helper exists — a hand-written
    # .set()/.reset() pair is easy to get wrong on the exception path. This
    # test pins the contract: both contextvars must be reset even when the
    # body raises, and the exception must still propagate to the caller.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom), execution_context(
        installation_id="inst-raise",
        act_chain=("user-raise",),
    ):
        assert current_installation_id.get() == "inst-raise"
        assert current_act_chain.get() == ("user-raise",)
        raise _Boom("body raised on purpose")

    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()


def test_execution_context_nested_unwinds_to_outer_values():
    # Nested scopes must restore the ENCLOSING values on inner-exit, not
    # the original defaults — this is the contract that makes it safe to
    # stack asApp delegations inside one another.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    with execution_context(
        installation_id="inst-outer",
        act_chain=("user-outer",),
    ):
        assert current_installation_id.get() == "inst-outer"
        assert current_act_chain.get() == ("user-outer",)

        with execution_context(
            installation_id="inst-inner",
            act_chain=("user-outer", "app-inner"),
        ):
            assert current_installation_id.get() == "inst-inner"
            assert current_act_chain.get() == ("user-outer", "app-inner")

        # After inner-exit, OUTER values are restored (not defaults).
        assert current_installation_id.get() == "inst-outer"
        assert current_act_chain.get() == ("user-outer",)

    # After outer-exit, original defaults are restored.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()


def test_execution_context_preserves_pre_existing_non_default_state():
    # If the caller's context already had non-default values (e.g. an
    # enclosing plain ``.set(...)`` call site that hasn't been migrated
    # to the helper yet), exiting the helper must restore those pre-
    # existing values — not the original module-level defaults.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    outer_install_token = current_installation_id.set("inst-pre-existing")
    outer_chain_token = current_act_chain.set(("user-pre-existing",))
    try:
        with execution_context(
            installation_id="inst-override",
            act_chain=("user-override", "app-override"),
        ):
            assert current_installation_id.get() == "inst-override"
            assert current_act_chain.get() == ("user-override", "app-override")

        # After the with-block, the PRE-EXISTING values are back, not
        # ``None`` / ``()``.
        assert current_installation_id.get() == "inst-pre-existing"
        assert current_act_chain.get() == ("user-pre-existing",)
    finally:
        current_act_chain.reset(outer_chain_token)
        current_installation_id.reset(outer_install_token)

    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()


def test_execution_context_accepts_none_installation_override():
    # ``installation_id=None`` is a VALID override — it explicitly
    # scopes "no installation in this sub-block" even when the outer
    # context had one. Proves the helper does not treat ``None`` as
    # "leave unchanged".
    outer_install_token = current_installation_id.set("inst-outer")
    try:
        with execution_context(
            installation_id=None,
            act_chain=("user-x",),
        ):
            assert current_installation_id.get() is None
            assert current_act_chain.get() == ("user-x",)

        # Outer value restored.
        assert current_installation_id.get() == "inst-outer"
    finally:
        current_installation_id.reset(outer_install_token)

    assert current_installation_id.get() is None


# ---------------------------------------------------------------------------
# SS-9 Slice A — async ``async_execution_context`` helper
# ---------------------------------------------------------------------------
#
# Slice A adds an ``@asynccontextmanager`` twin of the sync helper so async
# call sites can use ``async with async_execution_context(...):`` instead of
# nesting a sync ``with`` inside an async body. The contract mirrors the
# sync helper and these tests re-pin it at the async boundary:
#   * both vars bound inside the block and restored to defaults afterward;
#   * body-raised exceptions still trigger both resets;
#   * nested async scopes unwind to the ENCLOSING values (LIFO).


@pytest.mark.asyncio
async def test_async_execution_context_sets_both_and_restores_on_exit():
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    async with async_execution_context(
        installation_id="inst-async",
        act_chain=("user-a", "app-b"),
    ):
        assert current_installation_id.get() == "inst-async"
        assert current_act_chain.get() == ("user-a", "app-b")
        assert isinstance(current_act_chain.get(), tuple)

    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()


@pytest.mark.asyncio
async def test_async_execution_context_restores_both_when_body_raises():
    # Exception-safety at the async boundary — matches the sync helper's
    # promise. A hand-rolled async set/reset pair is easy to get wrong on
    # the exception path; this test pins the ``finally`` contract for the
    # async variant so future refactors cannot silently regress it.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with async_execution_context(
            installation_id="inst-async-raise",
            act_chain=("user-async-raise",),
        ):
            assert current_installation_id.get() == "inst-async-raise"
            assert current_act_chain.get() == ("user-async-raise",)
            raise _Boom("async body raised on purpose")

    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()


@pytest.mark.asyncio
async def test_async_execution_context_nested_unwinds_to_outer_values():
    # Nested ``async with`` scopes must restore the ENCLOSING values on
    # inner-exit, not the module defaults — the LIFO contract that makes
    # it safe to stack asApp delegations in async middleware.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    async with async_execution_context(
        installation_id="inst-async-outer",
        act_chain=("user-async-outer",),
    ):
        assert current_installation_id.get() == "inst-async-outer"
        assert current_act_chain.get() == ("user-async-outer",)

        async with async_execution_context(
            installation_id="inst-async-inner",
            act_chain=("user-async-outer", "app-async-inner"),
        ):
            assert current_installation_id.get() == "inst-async-inner"
            assert current_act_chain.get() == (
                "user-async-outer",
                "app-async-inner",
            )

        # After inner-exit, OUTER values are restored (not defaults).
        assert current_installation_id.get() == "inst-async-outer"
        assert current_act_chain.get() == ("user-async-outer",)

    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()
