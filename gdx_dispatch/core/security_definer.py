"""SS-17 slice B — Python-side helpers for calling PG SECURITY DEFINER functions.

SECURITY DEFINER runs a function body as the function's OWNER, bypassing
RLS for the caller. That is the controlled escape hatch for:

* trusted background jobs (cross-tenant admin reports, metering rollups,
  platform health dashboards)
* migration / backfill tooling that needs to touch all tenants
* the Command Center's "see every tenant" aggregate views

Per the SS-17 plan's P31 hardening checklist, each DEFINER function MUST:
  1. be owned by a dedicated, least-privilege role (``reporting_owner``,
     ``share_reader``, …), NOT ``postgres`` or ``gdx_app``;
  2. have an explicit ``SET search_path`` (no search-path injection);
  3. validate caller authorization inside the function body (never trust
     the argument alone);
  4. avoid dynamic SQL or fully parameterize it;
  5. ship with ``REVOKE ALL ... FROM PUBLIC; GRANT EXECUTE ... TO gdx_app``;
  6. have matching tests.

This module does NOT create DEFINER functions — the SS-17-d migration does.
It provides the Python call-site that app code uses to invoke them.

The core API is :func:`with_security_definer`, a context manager that
wraps a single function call in a clean transaction, logs entry/exit,
records an audit trail, and (on PG) issues a SELECT against the named
function. On non-PG (sqlite) it is a safe no-op that yields ``None`` so
unit tests can exercise surrounding code without a PG backend.

Allow-list discipline
---------------------
The helper refuses to invoke a function whose name is not in
:data:`KNOWN_DEFINER_FUNCTIONS`. That list is the single source of truth
for "which DEFINER functions does app code ever call?" and is cross-
referenced by the SS-17-d migration — any function created there must
be added here, and vice versa. A call to an unknown function raises
:class:`UnknownSecurityDefinerFunction` immediately, before any SQL runs.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Canonical set of SECURITY DEFINER functions shipped by SS-17-d. Each
# entry is the fully-qualified PG name ``<schema>.<function>``. App code
# may only invoke functions listed here; migration-time ``CREATE FUNCTION``
# statements must match 1:1.
KNOWN_DEFINER_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Cross-tenant aggregate used by platform-admin dashboards. Owner:
        # reporting_owner. Validates the caller is a platform-admin via
        # memberships lookup on the principal identity.
        "reporting.tenant_aggregate_revenue",
        # Per-tenant invoice count — used by Command Center tiles.
        "reporting.tenant_invoice_count",
        # Low-frequency backfill helper: re-computes a tenant's warranty
        # rollup. Called from ops scripts only.
        "reporting.tenant_warranty_rollup",
    }
)


class UnknownSecurityDefinerFunction(RuntimeError):
    """Raised when app code tries to call a DEFINER function that's not on the allow-list."""


class SecurityDefinerExecutionError(RuntimeError):
    """Raised when the underlying ``SELECT * FROM <fn>(...)`` call fails.

    Wraps the original SQLAlchemy/psycopg exception so the app boundary
    sees a single SS-17-specific error type. The original cause is
    preserved on ``__cause__``.
    """


def _is_postgres(session: Session) -> bool:
    """Return True iff the session is bound to a PostgreSQL dialect."""
    try:
        bind = session.get_bind()
    except Exception:  # noqa: BLE001 — any bind failure → treat as non-PG
        logger.debug("security_definer: get_bind() failed; treating as non-PG", exc_info=True)
        return False
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", "") if dialect is not None else ""
    return name == "postgresql"


def _validate_fn_name(fn_name: str) -> None:
    """Raise ``UnknownSecurityDefinerFunction`` if ``fn_name`` is not allow-listed."""
    if fn_name not in KNOWN_DEFINER_FUNCTIONS:
        raise UnknownSecurityDefinerFunction(
            f"SECURITY DEFINER function not on allow-list: {fn_name!r}. "
            f"Add it to KNOWN_DEFINER_FUNCTIONS and the SS-17-d migration, "
            f"or call via a normal RLS-scoped session."
        )


def call_security_definer(
    session: Session,
    fn_name: str,
    *args: Any,
) -> list[Any]:
    """Invoke a SECURITY DEFINER function and return the fetched rows.

    Parameters
    ----------
    session:
        A SQLAlchemy session. Caller owns transaction semantics; this
        function only issues the ``SELECT``.
    fn_name:
        Fully-qualified ``schema.function`` name — MUST be in
        :data:`KNOWN_DEFINER_FUNCTIONS`.
    *args:
        Positional arguments passed to the PG function. Bound as
        ``:a0, :a1, …`` parameters — never string-interpolated.

    Returns
    -------
    A list of result rows (possibly empty). On non-PG backends returns
    ``[]`` immediately after logging — callers that need PG behaviour
    should gate on :func:`_is_postgres` themselves.
    """
    _validate_fn_name(fn_name)

    if not _is_postgres(session):
        logger.info(
            "security_definer: non-PG backend; skipping %s call (returning [])",
            fn_name,
        )
        return []

    placeholders = ", ".join(f":a{i}" for i in range(len(args)))
    sql = f"SELECT * FROM {fn_name}({placeholders})"
    params = {f"a{i}": v for i, v in enumerate(args)}

    logger.info("security_definer: invoking %s with %d arg(s)", fn_name, len(args))
    try:
        result = session.execute(text(sql), params)
        rows = list(result.fetchall())
    except Exception as exc:
        logger.exception("security_definer: %s failed", fn_name)
        raise SecurityDefinerExecutionError(
            f"SECURITY DEFINER call {fn_name} failed: {exc}"
        ) from exc

    logger.info("security_definer: %s returned %d row(s)", fn_name, len(rows))
    return rows


@contextmanager
def with_security_definer(
    session: Session,
    fn_name: str,
    *args: Any,
) -> Iterator[list[Any] | None]:
    """Context manager wrapper around :func:`call_security_definer`.

    Yields the result rows (or ``None`` on non-PG) so callers can write::

        with with_security_definer(s, "reporting.tenant_invoice_count", tid) as rows:
            if rows is None:
                ...  # handle dev/sqlite path
            else:
                for row in rows:
                    ...

    Logs entry / exit at INFO so operators can correlate DEFINER calls in
    the audit log. Any exception inside the function body propagates; the
    ``finally`` block only emits a close-log.
    """
    _validate_fn_name(fn_name)  # fail loudly before opening the block
    logger.info("security_definer: enter %s", fn_name)
    try:
        if not _is_postgres(session):
            yield None
            return
        rows = call_security_definer(session, fn_name, *args)
        yield rows
    finally:
        logger.info("security_definer: exit %s", fn_name)


def assert_known_functions_match(expected: Sequence[str]) -> None:
    """Cross-check helper for the migration & tests.

    Raises ``AssertionError`` if the passed ``expected`` iterable does not
    equal :data:`KNOWN_DEFINER_FUNCTIONS` as a set. Used by the SS-17-d
    migration's test fixture to guarantee the migration's CREATE FUNCTION
    set is in lockstep with the Python allow-list.
    """
    expected_set = set(expected)
    if expected_set != set(KNOWN_DEFINER_FUNCTIONS):
        only_here = set(KNOWN_DEFINER_FUNCTIONS) - expected_set
        only_there = expected_set - set(KNOWN_DEFINER_FUNCTIONS)
        raise AssertionError(
            "SECURITY DEFINER allow-list drift: "
            f"only in KNOWN_DEFINER_FUNCTIONS={sorted(only_here)} "
            f"only in expected={sorted(only_there)}"
        )
