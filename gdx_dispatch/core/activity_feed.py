"""What belongs in a human-facing activity feed, and what is machine chatter.

This exists because the dashboard's "Recent Activity" card was effectively
empty. It asked ``/api/audit/logs`` for 50 rows and filtered them in the
browser — but on the live tenant **47 of the most recent 50 audit rows are
auth noise** (``token_refreshed`` alone was 2581 rows in 30 days). Only 3
survived the filter to fill a 10-row card, and during a busy auth window
nothing survived, so the widget silently fell through to an unrelated
jobs-list fallback.

Filtering in the browser cannot fix that: the noise is consumed from the same
50-row budget as the signal. The policy has to be applied in SQL, before the
LIMIT.

Two mechanisms:

* **Exclusion** — session/token churn is not activity. It is still in
  ``audit_logs`` (nothing is deleted; the table is append-only and the audit
  page can still show it), it just does not belong in a feed a human reads.
* **Collapsing** — editing a 12-line estimate emits 12 ``patch_line`` rows.
  Ten of them fill the card and say nothing. Consecutive rows for the same
  actor + action + subject collapse into one entry carrying a count.
"""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.audit import AuditLog

#: Routine session churn — high volume, zero information for an operator.
#: These names are the ones the live tenant actually writes, taken from
#: ``SELECT action, count(*) FROM audit_logs WHERE entity_type = 'auth'``,
#: not guessed:
#:
#:     logout                    10814
#:     token_refreshed            2935
#:     login                       166   (written alongside login_success)
#:     login_success               166
#:     refresh_replay_detected     825
#:     failed_login                 10
#:     login_failed                  9
#:     refresh_denied_db_verify      1
#:
#: Only the first three are hidden. `login` is suppressed because it is a
#: duplicate row written next to `login_success`, and `login_success` is the
#: one that survives.
FEED_HIDDEN_ACTIONS: frozenset[str] = frozenset(
    {
        "token_refreshed",
        "logout",
        "login",
        "session_renewed",
        "refresh_replay_within_leeway",
    }
)

#: Deliberately NOT a deny-list on ``entity_type``.
#:
#: An earlier version of this module hid ``entity_type='auth'`` wholesale with
#: a small allowlist of exceptions. routers/auth/core.py writes 13 distinct
#: actions under that type, so that hid the entire account-takeover chain —
#: password_reset_requested, password_reset_success, token_revoked,
#: user_sessions_revoked, refresh_replay_detected, login_blocked — behind a
#: filter whose stated purpose was to remove token churn.
#:
#: A successful login is also kept. For a small business the owner IS the
#: security team, and "someone signed in at 2am" is exactly the signal they can
#: act on; credential stuffing that SUCCEEDS produces no failed logins at all.
#: The volume argument does not apply either: 166 logins against 10,814
#: logouts and 2,935 token refreshes. Repetition is handled by collapse_runs,
#: not by hiding the event.
FEED_HIDDEN_ENTITY_TYPES: frozenset[str] = frozenset()


def feed_filter():
    """SQLAlchemy predicate for "rows a human should see".

    Applied before LIMIT so the page budget is spent on signal. Returns a
    clause, not a query, so callers keep control of their own scoping.
    """
    return AuditLog.action.notin_(tuple(FEED_HIDDEN_ACTIONS))


def _collapse_key(row: dict[str, Any]) -> tuple:
    return (
        row.get("user_id"),
        row.get("action"),
        row.get("entity_type"),
        row.get("entity_id"),
    )


def collapse_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge CONSECUTIVE rows with the same actor, action and subject.

    Editing a 12-line estimate writes 12 ``patch_line`` rows in a few seconds.
    Rendered one-per-line they fill the card and say nothing; collapsed they
    read as one edit with a count.

    Only consecutive runs merge, so the feed stays a truthful chronology — an
    unrelated event between two edits keeps them apart rather than silently
    reordering history. Rows are assumed to arrive newest-first, as every
    caller orders them; the surviving row is the newest of its run, and
    ``occurrence_count`` / ``first_occurred_at`` describe the rest.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if out and _collapse_key(out[-1]) == _collapse_key(row):
            prev = out[-1]
            prev["occurrence_count"] = prev.get("occurrence_count", 1) + 1
            # rows are newest-first, so each additional row is older
            prev["first_occurred_at"] = row.get("created_at")
            continue
        merged = dict(row)
        merged.setdefault("occurrence_count", 1)
        merged.setdefault("first_occurred_at", row.get("created_at"))
        out.append(merged)
    return out


#: How much to over-fetch before collapsing.
#:
#: collapse_runs necessarily runs after the query, so a page of N rows can
#: collapse to far fewer — a 40-line estimate edit collapses 20 rows to 1 and
#: leaves the card almost empty, which is the original starved-feed bug through
#: a different door. Fetching a multiple and trimming after the collapse keeps
#: the page full in the common case.
#:
#: This is a mitigation, not a guarantee: a page consisting entirely of one
#: enormous run still collapses to one entry. Doing it exactly needs a
#: windowed GROUP BY in SQL, which is not worth the complexity for a 10-row
#: dashboard card. `wanted_page_size` and `page_is_short` below let callers
#: reason about it rather than be surprised by it.
FEED_OVERFETCH = 5


def wanted_fetch_size(page_size: int, *, feed: bool) -> int:
    """Rows to ask the database for, given the caller wants `page_size` back."""
    return page_size * FEED_OVERFETCH if feed else page_size


def page_is_short(items: list[dict[str, Any]], page_size: int, fetched: int) -> bool:
    """True when collapsing left the page shorter than asked for AND the
    database had more rows to give. Callers can surface this instead of
    silently rendering a thin page."""
    return len(items) < page_size and fetched >= page_size
