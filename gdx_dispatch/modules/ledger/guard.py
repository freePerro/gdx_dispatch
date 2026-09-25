"""Flush-guard tripwire (S4, plan §S4 / spec §2).

A ``before_flush`` listener that catches Invoice.status writes which did NOT
go through ``transition_invoice_status`` — the runtime complement to the
static writer-inventory test. Once ``ledger_posting_enabled`` is on, a
bypassed status write is money silently escaping the ledger.

Behavior matrix:
- flag OFF (shipped default): guard is a no-op — every existing writer keeps
  its current behavior, zero overhead beyond the dirty-Invoice check.
- flag ON, dev/test: raise ``ChokepointBypassError`` — CI and local runs
  fail loudly.
- flag ON, prod-like: log an error and let the flush proceed — operational
  honesty over blowing up a paying user's request; the log line is the pager
  signal. ⚠ Prod runs with GDX_ENV UNSET today (app.py startup-encryption
  gate documents the convention), so *unset defaults to log-only*; raise
  mode requires an explicit dev/test env or a pytest run.

Install is idempotent and global (listens on the Session class); called from
``create_app()`` AND ``core/celery_app.py`` — the QB sync writes invoice
status from workers, the exact silent background path this tripwire exists
for.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger.service import (
    SANCTION_ATTR,
    SANCTION_REGISTRY_KEY,
    ledger_posting_enabled,
)

log = logging.getLogger(__name__)

_installed = False

_FLAG_CACHE_KEY = "gl_flush_guard_flag_cache"  # session.info: {company_id: bool}


class ChokepointBypassError(RuntimeError):
    """Invoice.status was written without transition_invoice_status while
    ledger posting is enabled."""


def _raise_mode() -> bool:
    """Raise only where a crash is a test failure, never a customer 500.
    Explicit GDX_ENV wins over the pytest heuristic so tests can exercise
    the log-only path."""
    env = os.getenv("GDX_ENV", "").strip().lower()
    if env in ("prod", "production", "staging"):
        return False
    if env in ("dev", "development", "test", "testing", "local"):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False  # unset = prod today (app.py convention) → log-only


def _flag_for(session: Session, company_id: str) -> bool:
    cache = session.info.setdefault(_FLAG_CACHE_KEY, {})
    if company_id not in cache:
        with session.no_autoflush:
            cache[company_id] = ledger_posting_enabled(session, company_id)
    return cache[company_id]


def _status_changed(obj) -> bool:
    history = inspect(obj).attrs.status.history
    return bool(history.has_changes())


def _consume_sanction(obj) -> bool:
    """A sanction is valid only for the exact status the chokepoint set
    (audit round 1: a bare boolean survived session.rollback() in the
    instance __dict__ and would have sanctioned a later raw write)."""
    sanctioned = getattr(obj, SANCTION_ATTR, None)
    if sanctioned is None:
        return False
    delattr(obj, SANCTION_ATTR)  # single-use either way
    return sanctioned == obj.status


def _check_flush(session: Session, _flush_context, _instances) -> None:
    # Lazy import: tenant_models is heavyweight and this listener runs on
    # every flush of every session in the process.
    from gdx_dispatch.models.tenant_models import Invoice

    offenders: list[str] = []
    for obj in session.dirty:
        if not isinstance(obj, Invoice) or not _status_changed(obj):
            continue
        if _consume_sanction(obj):
            continue
        if _flag_for(session, obj.company_id):
            offenders.append(f"invoice {obj.id} → {obj.status!r} (update)")
    for obj in session.new:
        if not isinstance(obj, Invoice):
            continue
        # Creation is chokepoint-free only for drafts; a non-draft birth is
        # an issuance that skipped P1 (QB pulls do this — disabled under the
        # flag in S9).
        if obj.status in (None, "draft") or _consume_sanction(obj):
            continue
        if _flag_for(session, obj.company_id):
            offenders.append(f"invoice {obj.id} born {obj.status!r} (create)")
    for obj in session.deleted:
        # Hard-deleting an invoice row under the flag erases money history
        # the ledger recorded — the app soft-deletes (deleted_at); a hard
        # delete is a bypass by definition.
        if isinstance(obj, Invoice) and _flag_for(session, obj.company_id):
            offenders.append(f"invoice {obj.id} hard-deleted")

    if not offenders:
        return
    message = (
        "Invoice.status written without transition_invoice_status while "
        f"ledger posting is enabled: {'; '.join(offenders)} — money is "
        "bypassing the ledger. Route the write through "
        "gdx_dispatch.modules.ledger.service.transition_invoice_status."
    )
    if _raise_mode():
        raise ChokepointBypassError(message)
    log.error("gl_chokepoint_bypass: %s", message)


def _sanctioned_write_survived(obj) -> bool:
    """Did the ``Invoice.status`` write this sanction blesses outlive the
    SAVEPOINT rollback that just happened?

    Asked of the instance, not of the transaction. SQLAlchemy restores its
    snapshot by EXPIRING (or expunging) exactly the instances whose changes
    the rollback discarded, so the instance already carries the answer and
    this never has to reason about savepoint depth.

    Everything is read through ``InstanceState``: ``state.dict`` is the raw
    instance ``__dict__``, so no branch here can trigger a lazy load —
    emitting SQL from a rollback listener would open a transaction.

    Conservative by construction: anything it cannot prove survived is
    treated as gone, so the sanction is cleared. This can only ever make the
    guard over-police, never under-police.
    """
    state = inspect(obj)
    if state.session is None or state.was_deleted:
        return False  # expunged by the rollback — the write went with it
    if "status" in state.unloaded:
        return False  # expired by the rollback — the in-memory write is gone
    return state.dict.get("status") == getattr(obj, SANCTION_ATTR, None)


def _clear_sanctions_on_rollback(session: Session, previous_transaction=None) -> None:
    """A rolled-back transition must not leave a live sanction behind (audit
    round 1) — but a SAVEPOINT rollback is not the business transaction
    rolling back, and must not strip sanctions it never unwound.

    ⚠ ``after_soft_rollback``, NOT ``after_rollback``. Same reasoning, and the
    same hook, as ``core/webhooks/emit.py``'s ``_drop_pending`` — read that
    one first; it is the precedent, and it was written after savepoint
    rollbacks made webhooks silently never fire on a fresh Postgres box.
    Measured again here on SQLAlchemy 2.0.54 (line numbers below are the
    installed sqlalchemy.orm.session module, not this repo):

    1. ``rollback()`` dispatches ``after_rollback`` at line 1366, *inside* the
       try, while ``_restore_snapshot()`` runs in the ``finally`` at 1371 — so
       an ``after_rollback`` listener runs before SQLAlchemy has expired
       anything and cannot tell an unwound write from a surviving one.
       ``after_soft_rollback`` (1400) is the last statement of ``rollback()``,
       after both the restore and the close.
    2. ``after_rollback`` only fires from the ``ACTIVE``/``PREPARED`` branch,
       so a ``session.rollback()`` on a transaction a failed flush already
       deactivated never reaches it. ``after_soft_rollback`` fires for every
       rollback — a strict superset.
    3. ``session.in_transaction()`` is NOT a usable discriminator in either
       hook: ``rollback()`` dispatches before ``close()``, so it reads True
       even for a root rollback. ``previous_transaction.nested`` is the one
       that works, which is why ``_drop_pending`` uses it.

    Where this guard has to go further than ``_drop_pending``: that one can
    return outright on a savepoint rollback, because staged webhook payloads
    only die with the business write. A sanction minted INSIDE the savepoint
    *is* genuinely undone by rolling it back, and leaving it live would bless
    a later raw write to the same status — audit round 1's finding, at
    savepoint depth. So the nested branch filters rather than skips.

    GDXA-51 / GDXA-47: the old form listened on ``after_rollback`` and popped
    the whole registry, which holds only if the root is the only rollback that
    can reach it. It is not — ``rollback()`` dispatches for every transaction
    where ``_parent is None or nested``, and the money path's savepoint sites
    are ``engine.post_event``'s idempotency-key retry and
    ``core/payments.py``'s audit savepoint.

    Latent today, deliberately fixed anyway: no shipping caller can present a
    live sanction here, because ``SessionTransaction._take_snapshot`` flushes
    on every ``begin_nested()`` (same module, line 1090) and that flush
    spends the
    stamp before the savepoint exists. That is SQLAlchemy's accident, not this
    module's invariant.
    """
    if not getattr(previous_transaction, "nested", False):
        # The business transaction is gone; nothing it staged survives. Also
        # the registry's ONLY drain — commit and close dispatch nothing here.
        for obj in session.info.pop(SANCTION_REGISTRY_KEY, ()):  # noqa: B020
            if getattr(obj, SANCTION_ATTR, None) is not None:
                delattr(obj, SANCTION_ATTR)
        return

    registry = session.info.get(SANCTION_REGISTRY_KEY)
    if not registry:
        return

    survivors = []
    for obj in registry:
        if getattr(obj, SANCTION_ATTR, None) is None:
            continue  # already spent by a flush — drop it from the registry
        if _sanctioned_write_survived(obj):
            survivors.append(obj)  # this savepoint did not unwind it
        else:
            delattr(obj, SANCTION_ATTR)

    if survivors:
        session.info[SANCTION_REGISTRY_KEY] = survivors
    else:
        session.info.pop(SANCTION_REGISTRY_KEY, None)


def install_flush_guard() -> None:
    """Idempotent, process-global. Listening on the Session *class* covers
    every session this process creates."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _check_flush)
    event.listen(Session, "after_soft_rollback", _clear_sanctions_on_rollback)
    _installed = True
