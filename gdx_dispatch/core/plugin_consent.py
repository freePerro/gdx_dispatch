"""Owner consent for elevated plugin permissions (ADR-014).

A plugin may declare `permissions` in its manifest (e.g. "browser" — a streamed
headless browser the operator drives; "events"/"schedules"/"services" — automatic
execution). Those are powerful, so before the capability can be used, an owner
must explicitly consent after reading the risk. Consent records WHICH permissions
were granted, so if a plugin later adds a new permission the old consent doesn't
silently cover it.

For the event platform, consent additionally pins the plugin's declared
*automatic-execution surface*: the exact event list (the fingerprint preimage)
plus a fingerprint of (events, schedule-names, services). The core fan-out
enumerates event recipients from the STORED event list and uses the live catalog
only to detect drift — so a compromised/edited plugin-host can't expand what a
plugin receives, and a plugin upgrade that changes its declared events
fail-closes dispatch until the owner re-consents.
"""
from __future__ import annotations

import json
import logging
import os

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core import internal_auth

# HARD DEPENDENCY on GDXA-86: `contained_read` landed with it, and this module
# must not ship without it. Module scope on purpose, and the blast radius is
# worth knowing rather than discovering, because it is NOT all in one place:
#
#   - the API half degrades quietly. `app.py` wraps browser_proxy +
#     plugins_proxy + admin_plugins in ONE try/except, so an ImportError here
#     does not crash the app: it logs "plugins proxy router failed to load" and
#     boots green with three whole routers missing. That is 25 of the operations
#     pinned in `openapi_routes.txt` — admin_plugins 11, plugins_proxy 10
#     (two `api_route` decorators carrying 5 methods each), browser_proxy 4 —
#     plus one websocket, which OpenAPI does not list.
#   - the WORKER half does not degrade at all, it crash-loops.
#     `core/celery_app.py` imports `core.plugin_events` at module scope with no
#     guard, and that imports this module, so celery-high, celery-low and
#     celery-beat all fail to start — under `restart: unless-stopped` that is a
#     restart loop, which is the loud half and the reason this is bearable.
#
# A function-local import would be worse, not better: `any_event_consent`'s own
# `except Exception: return False` would swallow it and silently turn off plugin
# event fan-out with no log line anywhere. The real guard is that
# `tests/test_plugin_consent_contained_read.py` fails at COLLECTION without this
# name — which only holds while that file is TRACKED, so a path-limited commit
# in this shared worktree must include it.
from gdx_dispatch.core.database import contained_read
from gdx_dispatch.plugin_api.events import capability_fingerprint, event_matches

log = logging.getLogger(__name__)


def _plugin_host_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


def internal_auth_headers() -> dict[str, str]:
    """Header carrying the shared internal token for plugin-host /internal/* calls.

    Empty when no token is configured. Since #596 plugin-host FAILS CLOSED, so an
    empty header set in an enforcing environment means every /internal/* call
    gets a 401 — a real misconfiguration, not a quiet degrade. Log it loudly
    here, at the caller, because that is the side that can name which container
    is missing the variable; the 401 on its own only says "someone was refused".
    """
    tok = internal_auth.token()
    if not tok:
        if internal_auth.enforced():
            # Neither an explicit GDX_INTERNAL_TOKEN nor a SECRET_KEY to derive
            # one from. SECRET_KEY has a compose default, so reaching here means
            # the container was started outside compose or with it blanked.
            log.error(
                "No internal token available in this container (neither "
                "GDX_INTERNAL_TOKEN nor SECRET_KEY is set) but plugin-host "
                "enforces it (GDX_ENV=%s) — every /internal/* call will 401. "
                "app, plugin-host and celery-high must all hold the SAME value; "
                "the compose files have no env_file, so an explicit override "
                "belongs in the shared x-app-env block.",
                os.getenv("GDX_ENV", ""),
            )
        return {}
    return {internal_auth.INTERNAL_TOKEN_HEADER: tok}


def ensure_consent_table(db: Session) -> None:
    """Create plugin_consent (and backfill its two later columns) if absent.

    **It COMMITS the caller's transaction, unconditionally.** That is the hazard
    to know about this function, so it goes first: anything the caller has staged
    is hardened whether it meant it or not. Nothing enforces the precondition, so
    it was checked rather than assumed — three direct callers
    (``record_consent``, ``consented_permissions``, ``event_recipients``), five
    entry points through them, and none holds pending work when it arrives:
    ``admin_plugins.py:411``'s consent route is *built* around this commit and
    stages its grant afterwards; ``admin_plugins.py:382``'s ``plugin_permissions``
    and ``browser_proxy.py:78``'s browser gate only read; and both
    ``tasks/plugin_email_outbox.py:59`` and ``core/plugin_events.py:91``'s
    dispatch task run on a session they opened themselves. **A future caller
    that arrives with work of its own is the thing to look at here, not the
    swallow below — and the one that has already FLUSHED it is the dangerous
    one, not the one still holding it.** ``any_event_consent`` deliberately does
    NOT call this at all, precisely because that one runs inside a money
    transaction.

    The commit is also why it cannot be contained: transaction control inside a
    savepoint releases the very savepoint meant to contain the work, the same
    reason ``core/audit.py`` hoists ``ensure_audit_table`` outside
    ``audit_best_effort``'s ``begin_nested()`` (documented there at point (1)).
    So ``core.database.contained_read`` is the wrong tool here and no
    containment is being withheld.

    That matters because the swallowed ``db.execute`` below has the GDXA-86
    census shape — caller-owned session, statement in a ``try``, swallowing
    ``except`` — without the defect, and the commit is why. That class's harm is
    handing a caller back a silently poisoned session; this commit ENDS the
    aborted transaction, so nobody is handed one. Measured on PG 15.17 (prod is
    16.13) by making the ALTER fail for real, with a VIEW named plugin_consent
    that ``CREATE TABLE IF NOT EXISTS`` skips over without an error, in all three
    caller shapes:

    1. nothing staged — returns quietly leaving the session usable, and
       ``record_consent`` then raises ``UndefinedColumn`` on its own INSERT.
    2. work still pending — the flush inside the commit raises
       ``InFailedSqlTransaction`` out of this frame: loud, in the right place.
    3. work already FLUSHED — **the bad one.** Nothing is left to flush, so the
       commit raises nothing; the transaction is already aborted, and Postgres
       answers COMMIT on an aborted transaction with a ROLLBACK rather than an
       error. So this returns quietly, the caller's own ``commit()`` reports
       success, and the caller's row is gone. A silent write loss.

    So do not read the commit as making this function safe — it only keeps it out
    of the GDXA-86 class, which is a claim about *which* defect this is, not a
    clean bill of health. Shape 3 is not reachable from any of the five entry
    points above (each was re-walked, not assumed); it is the precondition a
    future caller must not break. Shape 3 was found by this change's adversarial
    audit, after the first two had been measured and written up here as the whole
    story — so if you are adding a caller, measure rather than trust this list.
    All three are pinned by ``tests/test_plugin_consent_contained_read.py::
    test_pg_ensure_consent_table_does_not_hand_on_a_poisoned_session``.
    """
    # Fresh tables (tests, new deploys) get the event-platform columns inline.
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS plugin_consent (
                plugin_key           TEXT PRIMARY KEY,
                permissions          TEXT NOT NULL,
                consented_by         TEXT,
                consented_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                declared_events      TEXT,
                declared_fingerprint TEXT
            )
            """
        )
    )
    # Existing prod table predates the two columns — add them idempotently.
    # declared_events = JSON list of consented event patterns (preimage);
    # declared_fingerprint = capability_fingerprint at consent time.
    for col in ("declared_events", "declared_fingerprint"):
        try:
            db.execute(text(f"ALTER TABLE plugin_consent ADD COLUMN IF NOT EXISTS {col} TEXT"))
        except Exception as exc:
            # Column already present, or an SQLite build without IF NOT EXISTS
            # where CREATE already made it — either way it exists now. Name the
            # cause: on Postgres IF NOT EXISTS makes the benign case a no-op, so
            # a failure HERE is something else, and without this the only trace
            # is the opaque InFailedSqlTransaction raised by the commit below.
            log.debug("plugin_consent add-column skipped col=%s err=%s", col, exc)
    db.commit()


def fetch_catalog() -> list[dict]:
    """The live plugin catalog from plugin-host (key/permissions/events/…)."""
    try:
        r = httpx.get(f"{_plugin_host_url()}/api/plugins", timeout=5.0)
        return list(r.json())
    except Exception:
        return []


def fetch_permissions(key: str) -> list[str]:
    """The permissions a plugin currently declares (from the plugin-host catalog)."""
    for p in fetch_catalog():
        if p.get("key") == key:
            return list(p.get("permissions") or [])
    return []


def _live_entry(key: str, catalog: list[dict] | None = None) -> dict | None:
    for p in catalog if catalog is not None else fetch_catalog():
        if p.get("key") == key:
            return p
    return None


def live_fingerprint(entry: dict) -> str:
    """capability_fingerprint of a live catalog entry (names only)."""
    return capability_fingerprint(
        events=entry.get("events") or (),
        schedule_names=entry.get("schedules") or (),
        services=entry.get("services") or (),
    )


def record_consent(db: Session, key: str, permissions: list[str], by: str,
                   commit: bool = True) -> dict:
    """Record consent for a plugin's currently-declared permissions AND pin its
    declared event surface (preimage + fingerprint) from the live catalog.

    Pass ``commit=False`` to leave the INSERT pending so the caller can commit it
    together with something else — the consent audit row, in particular. Note
    ``ensure_consent_table`` commits its DDL, so anything the caller has already
    flushed is committed before this INSERT runs: stage the row first, audit
    after, commit once.
    """
    ensure_consent_table(db)
    entry = _live_entry(key) or {}
    declared_events = list(entry.get("events") or [])
    fingerprint = live_fingerprint(entry) if entry else ""
    db.execute(
        text(
            """
            INSERT INTO plugin_consent
                (plugin_key, permissions, consented_by, declared_events, declared_fingerprint)
            VALUES (:k, :p, :by, :ev, :fp)
            ON CONFLICT (plugin_key) DO UPDATE
              SET permissions = EXCLUDED.permissions,
                  consented_by = EXCLUDED.consented_by,
                  consented_at = CURRENT_TIMESTAMP,
                  declared_events = EXCLUDED.declared_events,
                  declared_fingerprint = EXCLUDED.declared_fingerprint
            """
        ),
        {"k": key, "p": ",".join(permissions), "by": by,
         "ev": json.dumps(declared_events), "fp": fingerprint},
    )
    if commit:
        db.commit()
    return {"declared_events": declared_events, "declared_fingerprint": fingerprint}


def consented_permissions(db: Session, key: str) -> set[str]:
    ensure_consent_table(db)
    row = db.execute(
        text("SELECT permissions FROM plugin_consent WHERE plugin_key = :k"), {"k": key}
    ).first()
    if not row or not row[0]:
        return set()
    return {p.strip() for p in row[0].split(",") if p.strip()}


def has_permission_consent(db: Session, key: str, permission: str) -> bool:
    return permission in consented_permissions(db, key)


def any_event_consent(db: Session) -> bool:
    """Cheap gate: is ANY plugin consented to receive events? Lets the emit path
    skip staging plugin dispatch entirely on the (common) zero-plugin box.

    READ-ONLY on purpose: this runs inside the caller's business transaction
    (the money/job choke points). It must NEVER create the table or commit —
    ensure_consent_table() commits, which would commit the caller's half-built
    invoice/job. A missing table (no plugin ever consented) just means False.

    SAVEPOINT-wrapped: plugin_consent is a lazily-created raw table (not an ORM
    model), so on a FRESH Postgres box it doesn't exist yet — a bare SELECT would
    raise UndefinedTable and POISON the caller's transaction, failing the
    invoice/job/customer commit. (SQLite doesn't poison, so this bug is
    Postgres-only and invisible to the SQLite test harness.)

    The savepoint comes from ``core.database.contained_read``, deliberately NOT
    from ``db.begin_nested()`` — which is what this used first, and which
    contains the UndefinedTable perfectly well but not the case that matters
    here (GDXA-137). ``SessionTransaction._take_snapshot`` FLUSHES on every
    ``begin_nested()``, and that flush runs while the transaction object is
    still being constructed, i.e. **before the savepoint exists**. So a caller
    holding a row that cannot flush never enters the block, no savepoint is ever
    emitted, and the abort happens upstream of any containment. Measured on PG
    15.17 with the caller's own table renamed out from under it:

    - ``begin_nested()`` — the probe dies in the snapshot flush before reading
      anything, ``db.new`` is emptied, ``is_active`` goes False, the ``except``
      below swallows the only evidence, and the caller's ``commit()`` raises
      ``PendingRollbackError`` naming no cause.
    - ``contained_read`` — no flush here at all: ``db.new`` is still 1, the
      session is still active, and the caller's ``commit()`` raises its OWN
      ``UndefinedTable``, which it can act on.

    The sharpest version is a caller whose pending row is merely a DUPLICATE KEY
    — an error it was about to handle. Same box: with ``begin_nested()`` the
    snapshot flush raises that ``IntegrityError`` *inside this function*, the
    ``except`` below swallows it, and the caller's ``commit()`` raises
    ``PendingRollbackError`` instead, so the caller's own ``except
    IntegrityError`` never runs. With ``contained_read`` the caller's
    ``commit()`` raises the ``IntegrityError`` it was waiting for. Both are
    pinned in ``tests/test_plugin_consent_contained_read.py``.

    Every measurement above is PG 15.17 (the test container). Prod is 16.13, so
    read them as the mechanism, not as a reproduction of a prod incident — none
    has been reported, and the docstring of ``contained_read`` enumerates how
    narrow the trigger is on prod's current settings.

    **Which live callers this actually protects: two of the nine, and they are
    estimate send and estimate accept/decline.** Measured 2026-09-27 by
    instrumenting this function and driving the real routes, not by reading them
    — a static read of these same nine sites got this WRONG in an earlier draft
    of this very docstring, which is why the method is named here and not just
    the result:

    - ``routers/estimates.py:2190`` (send_estimate) — 18 calls, **0 clean**: a
      dirty, unflushed persistent ``Estimate`` (status, sent_at, sent_via,
      valid_until, updated_at) is held at the probe. Its first ``db.commit()``
      is line 2205, *after* the emit.
    - ``routers/estimates.py:276`` (``_emit_estimate_decision`` →
      ``estimate.accepted`` / ``estimate.declined``) — 12 calls, **0 clean**.
    - the other seven arrive clean: ``estimates.py:2988`` commits first,
      ``customers.py:471`` flushes deliberately (``Customer.id`` is a flush-time
      default), and ``jobs.py``, ``transactional_email.py``,
      ``ledger/service.py`` (via ``post_for_event``, which flushes inside its own
      savepoint) and ``bounce_detect.py`` all flush first.

    ``routers/estimates.py`` contains **zero** ``SQLAlchemyError`` handlers and
    its ``db.commit()`` at 2205 is unguarded, so before this change a failed
    snapshot flush inside this probe was eaten by emit's
    ``log.exception("plugin_dispatch_stage_failed")`` and the route then died on
    an unhandled ``PendingRollbackError`` naming nothing at all. Still narrow —
    the flush itself has to fail, and ``contained_read``'s docstring enumerates
    how little can make that happen on prod's current settings, which is why the
    issue is priority ``low``. But do not read it as cosmetic, and do not cite a
    caller census as grounds for putting ``begin_nested()`` back: re-measure
    instead. This census has already been wrong once.

    **This fixes the probe, not ``emit_domain_event``'s whole promise, and the
    difference is one active webhook subscription.** ``_emit`` stages each
    delivery row inside its own ``db.begin_nested()`` + ``flush()`` BEFORE
    reaching here, so on a box with a subscription matching the event, a caller
    whose row cannot flush is already poisoned upstream of this function and its
    ``commit()`` still dies with ``PendingRollbackError``. Measured end to end
    through ``emit_domain_event`` on PG 15.17: no subscription → the caller gets
    its own ``UndefinedTable``; with one → the pre-fix outcome. That staging
    savepoint is a WRITE, so it genuinely wants ``begin_nested()`` and cannot
    simply take ``contained_read`` — untangling it is platform-core's change in
    ``core/webhooks/emit.py``, not this one. Pinned as a known limit by
    ``test_pg_a_matching_subscription_still_poisons_upstream_of_this_probe``, so
    the next reader does not have to rediscover it. What this function is
    responsible for is being the probe that no longer adds a failure of its own,
    and on the zero-subscriber box — the ordinary state of this install, which is
    why emit's own notes call it that — it is also the last one in the way.

    ``contained_read`` opens its SAVEPOINT on the *Connection*, below the ORM,
    and holds ``no_autoflush`` — which is exactly why there is no flush, and
    also why it is reads only. This function is a pure read; it must stay one.
    A connection-level savepoint is invisible to the Session, so unlike
    ``begin_nested()`` this probe no longer fires the session's ``after_commit``
    / ``after_soft_rollback`` listeners. That is a narrowing, not a fix: the
    GDXA-50 guards in ``core/webhooks/emit.py`` are still load-bearing and are
    still reached, by emit's own per-row delivery-staging savepoint. Measured
    both ways, with the net counts, at the two tests named in
    ``tests/test_webhooks_emit.py``.
    """
    try:
        with contained_read(db):
            row = db.execute(
                text(
                    "SELECT 1 FROM plugin_consent "
                    "WHERE declared_events IS NOT NULL AND declared_events NOT IN ('', '[]') "
                    "LIMIT 1"
                )
            ).first()
        return row is not None
    except Exception:
        return False


def event_recipients(db: Session, event_name: str) -> tuple[list[str], list[str]]:
    """Return (recipients, drifted) for an event.

    recipients — plugin keys the owner consented to for this event, enumerated
      from the STORED declared_events preimage, and whose live catalog
      fingerprint still matches the consented one.
    drifted    — plugin keys that WOULD match but whose live fingerprint differs
      from the consented one (plugin changed its declared surface). Dispatch
      fail-closes for these; the caller raises a loud owner signal.
    """
    ensure_consent_table(db)
    rows = db.execute(
        text(
            "SELECT plugin_key, declared_events, declared_fingerprint, permissions "
            "FROM plugin_consent "
            "WHERE declared_events IS NOT NULL AND declared_events NOT IN ('', '[]')"
        )
    ).all()
    if not rows:
        return [], []
    catalog = fetch_catalog()
    recipients: list[str] = []
    drifted: list[str] = []
    for key, ev_json, stored_fp, perms in rows:
        # Defense-in-depth: the owner must have consented the 'events' permission,
        # not merely have a non-empty event list. (Manifest validation couples
        # them, but don't rely on that transitively.)
        if "events" not in {p.strip() for p in (perms or "").split(",")}:
            continue
        try:
            patterns = json.loads(ev_json) if ev_json else []
        except (json.JSONDecodeError, TypeError):
            continue
        if not event_matches(event_name, patterns):
            continue
        entry = _live_entry(key, catalog)
        if entry is None:
            continue  # plugin not currently loaded — nothing to deliver to
        if live_fingerprint(entry) != (stored_fp or ""):
            drifted.append(key)  # declared surface changed → fail closed
            continue
        recipients.append(key)
    return recipients, drifted
