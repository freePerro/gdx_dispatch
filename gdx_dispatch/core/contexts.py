"""SS-8 Slice B + D — execution-context shells for asUser / asApp flows.

Storage-only ContextVars that downstream SS-8 slices populate from JWT
validation, signed installation tokens, and the asApp delegation chain.
This module intentionally provides ONLY the typed storage primitives
plus a single scoped-override helper — no JWT plumbing, no FastAPI
dependency, no router wiring — so the contract stays small enough to
verify with deterministic unit tests.

Field shapes mirror ``gdx_dispatch.core.principal.Principal`` SS-8 seed fields
(landed in Slice A) so a future ``Principal`` instance constructed from
context state stays type-compatible:

* ``current_installation_id: ContextVar[str | None]`` — default ``None``
  (no installation in scope; equivalent to "this is a plain user
  request, not an asApp delegation").
* ``current_act_chain: ContextVar[tuple[str, ...]]`` — default ``()``,
  IMMUTABLE so the contextvar default cannot be mutated by accident
  (matches the ``Principal.act_chain`` frozen-dataclass contract).

Callers must use the standard ContextVar lifecycle — capture the token
returned by ``.set(...)`` and pass it to ``.reset(token)`` to restore
the previous value. Async callers get task isolation for free because
``asyncio`` copies the current context on task creation.

Slice D adds :func:`execution_context`, a ``@contextmanager`` that sets
BOTH contextvars together and guarantees ``reset(token)`` on exit
(including exceptions). Later SS-8 slices (auth dependency, signed
installation-token validation) call this helper from a single site so
no request path forgets the reset half of the lifecycle. The helper
stays storage-only — it does not know about FastAPI, request scope,
or JWT — and it does not mutate ``Principal``. Slice C already plumbs
the contextvar values onto ``Principal`` at JWT validation time.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

current_installation_id: ContextVar[str | None] = ContextVar(
    "gdx_current_installation_id", default=None
)

current_act_chain: ContextVar[tuple[str, ...]] = ContextVar(
    "gdx_current_act_chain", default=()
)


@contextmanager
def execution_context(
    *,
    installation_id: str | None,
    act_chain: tuple[str, ...],
) -> Iterator[None]:
    """Set both Slice B contextvars together for the duration of the block.

    Captures the tokens returned by ``current_installation_id.set(...)``
    and ``current_act_chain.set(...)`` and guarantees both are restored
    via ``reset(token)`` in the ``finally`` block, even if the caller's
    code raises. Restoration is LIFO: ``act_chain`` is reset before
    ``installation_id`` so nested scopes unwind exactly the way the
    stack was built.

    Parameters
    ----------
    installation_id:
        Value to bind to :data:`current_installation_id` inside the
        block. ``None`` is a valid override — it explicitly records
        "no installation in scope" for the nested code even if the
        outer context had one. Both keyword args are REQUIRED to make
        call sites think about the bundled pair rather than silently
        leaving one contextvar stale.
    act_chain:
        Value to bind to :data:`current_act_chain` inside the block.
        Must be a ``tuple[str, ...]`` to match the contextvar's
        immutable-default contract and the matching
        ``Principal.act_chain`` field shape.

    Yields
    ------
    None
        The helper is a pure scoping primitive; there is no returned
        handle because callers read state via the module-level
        contextvars (and Slice C's ``validate_access_token`` plumbs
        them onto ``Principal``).

    Notes
    -----
    * Exception-safe: if the caller's body raises, the ``finally``
      block still fires both ``.reset(token)`` calls before the
      exception propagates.
    * Nested-scope-safe: each enter stacks new tokens; each exit
      resets them in reverse order, restoring whatever values were
      live at the enclosing scope (tested via ``Slice B`` primitives
      and reinforced by this slice's nested test).
    * Sync-only: callers inside async code may still use this
      helper — ``asyncio.create_task`` copies the current context,
      so scopes opened on the current task remain task-local. An
      ``asynccontextmanager`` variant is deliberately deferred to a
      later slice; the underlying contextvar ops are non-blocking
      and do not need one.
    """
    installation_token = current_installation_id.set(installation_id)
    act_chain_token = current_act_chain.set(act_chain)
    try:
        yield
    finally:
        # LIFO unwind: reset the second-set var first so nested scopes
        # exit the stack in the exact reverse order they were pushed.
        current_act_chain.reset(act_chain_token)
        current_installation_id.reset(installation_token)


@asynccontextmanager
async def async_execution_context(
    *,
    installation_id: str | None,
    act_chain: tuple[str, ...],
) -> AsyncIterator[None]:
    """Async twin of :func:`execution_context` — same contract, ``async with``.

    Mirrors :func:`execution_context` exactly: sets both SS-8 contextvars
    on enter, guarantees ``reset(token)`` on exit (including exceptions),
    and unwinds LIFO — ``act_chain`` reset before ``installation_id`` —
    so nested ``async with`` scopes restore their enclosing values rather
    than collapsing to the module defaults.

    The sync helper is already safe from inside async code because
    ``asyncio.create_task`` snapshots the current context. This async
    variant exists for call sites that want the same scoped-override
    ergonomics inside an ``async with`` block — for example async
    middleware or async FastAPI dependencies that prefer not to nest a
    sync ``with execution_context(...)`` inside their async body.

    Parameters
    ----------
    installation_id:
        Value to bind to :data:`current_installation_id` inside the
        block. ``None`` is a valid override (explicitly scopes "no
        installation" even when the outer context had one). Keyword-only
        to match the sync helper's "bundled pair" call-site discipline.
    act_chain:
        Value to bind to :data:`current_act_chain` inside the block.
        Must be ``tuple[str, ...]`` — same immutable-default contract as
        the contextvar and ``Principal.act_chain``.

    Yields
    ------
    None
        Pure scoping primitive — callers read state via the module-level
        contextvars, identical to the sync helper.

    Notes
    -----
    * Exception-safe: the ``finally`` block fires both ``.reset(token)``
      calls before any exception raised inside ``async with`` propagates.
    * Nested-scope-safe: LIFO unwind matches the sync helper.
    * The sync helper remains the preferred default; use this variant
      only when an ``async with`` reads more naturally than wrapping the
      sync ``with`` inside an async body.
    """
    installation_token = current_installation_id.set(installation_id)
    act_chain_token = current_act_chain.set(act_chain)
    try:
        yield
    finally:
        # LIFO unwind: reset the second-set var first so nested scopes
        # exit the stack in the exact reverse order they were pushed.
        current_act_chain.reset(act_chain_token)
        current_installation_id.reset(installation_token)
