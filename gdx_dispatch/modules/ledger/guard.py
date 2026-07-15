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


def _clear_sanctions_on_rollback(session: Session) -> None:
    """A rolled-back transition must not leave a live sanction behind
    (audit round 1). The service registers every stamped instance here."""
    for obj in session.info.pop(SANCTION_REGISTRY_KEY, ()):  # noqa: B020
        if getattr(obj, SANCTION_ATTR, None) is not None:
            delattr(obj, SANCTION_ATTR)


def install_flush_guard() -> None:
    """Idempotent, process-global. Listening on the Session *class* covers
    every session this process creates."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _check_flush)
    event.listen(Session, "after_rollback", _clear_sanctions_on_rollback)
    _installed = True
