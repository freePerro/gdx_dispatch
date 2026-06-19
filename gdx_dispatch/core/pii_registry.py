"""SS-35 slice A — declarative PII field registry.

A central, process-wide registry that maps each registered (table, column)
tuple to a PII category and optional retention policy. The registry is the
single source of truth for Subject Access Requests (SAR) and right-to-erasure
sweeps — both walk this registry rather than annotations on the ORM models,
so that the privacy surface is auditable in one place.

Public surface
--------------
- :class:`PIIField` — immutable declarative record.
- :func:`register_pii_field` — register a (table, column, category) with
  optional retention.
- :func:`list_pii_fields` — enumerate registrations, filter by table.
- :func:`get_pii_for_identity` — walk every registration that references
  an identity (via explicit ``identity_fk_column``) and return the current
  value + provenance per field.
- :func:`clear_registry` — test helper; not intended for production use.

Categories
----------

``contact`` | ``identity`` | ``financial`` | ``health`` | ``location`` |
``behavioral`` | ``technical``

Erasure scrub behaviour is driven by category (see
:mod:`gdx_dispatch.core.erasure_executor`). SAR builder treats all categories the
same but groups them in the output for readability
(see :mod:`gdx_dispatch.core.sar_builder`).

Design notes
------------

The registry is *declarative*: registrations are done at import time in
:mod:`gdx_dispatch.core.pii_fields`. SS-35 deliberately does NOT annotate model
columns inline — we want the privacy surface to be one file, not 30.

RLS note (SS-17): reads executed by this module are expected to run under
the security-definer reporting role, because SAR + erasure are
super-admin surfaces that cross tenant boundaries. Callers are
responsible for establishing the correct session role before invoking
:func:`get_pii_for_identity`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

VALID_CATEGORIES = (
    "contact",
    "identity",
    "financial",
    "health",
    "location",
    "behavioral",
    "technical",
)


@dataclass(frozen=True)
class PIIField:
    """Immutable declaration of a PII-carrying column."""

    table: str
    column: str
    pii_category: str
    retention_days: Optional[int] = None
    #: Column (on ``table``) that references the identity. If None, the
    #: table IS the identity table itself and its primary key ``id`` is
    #: used as the identity reference.
    identity_fk_column: Optional[str] = None
    #: Scrub strategy override — "null" replaces with NULL, "erased"
    #: replaces with the literal string ``"[ERASED]"``, "skip" leaves
    #: the value in place (for immutable financial ledgers, etc.).
    #: Default strategy is chosen by category in :mod:`erasure_executor`.
    scrub_strategy: Optional[str] = None
    notes: Optional[str] = None


_LOCK = RLock()
_REGISTRY: Dict[Tuple[str, str], PIIField] = {}


def register_pii_field(
    table: str,
    column: str,
    pii_category: str,
    retention_days: Optional[int] = None,
    identity_fk_column: Optional[str] = None,
    scrub_strategy: Optional[str] = None,
    notes: Optional[str] = None,
) -> PIIField:
    """Register a PII field. Later registrations for the same
    ``(table, column)`` overwrite earlier ones deterministically — this
    keeps tests from leaking state between modules.

    Raises ``ValueError`` on unknown category or bad scrub strategy.
    """
    if pii_category not in VALID_CATEGORIES:
        raise ValueError(
            f"unknown pii_category={pii_category!r} "
            f"(valid: {VALID_CATEGORIES})"
        )
    if scrub_strategy is not None and scrub_strategy not in (
        "null",
        "erased",
        "skip",
    ):
        raise ValueError(f"unknown scrub_strategy={scrub_strategy!r}")

    rec = PIIField(
        table=table,
        column=column,
        pii_category=pii_category,
        retention_days=retention_days,
        identity_fk_column=identity_fk_column,
        scrub_strategy=scrub_strategy,
        notes=notes,
    )
    with _LOCK:
        _REGISTRY[(table, column)] = rec
    return rec


def list_pii_fields(table: Optional[str] = None) -> List[PIIField]:
    """Return registered PII fields. If ``table`` is provided, filter."""
    with _LOCK:
        recs = list(_REGISTRY.values())
    if table is not None:
        recs = [r for r in recs if r.table == table]
    # Deterministic order makes test assertions easier.
    return sorted(recs, key=lambda r: (r.table, r.column))


def clear_registry() -> None:
    """Clear the registry. Test-only; production code never calls this."""
    with _LOCK:
        _REGISTRY.clear()


# ───────────────────────── identity walk ─────────────────────────


def _identity_column_for(rec: PIIField) -> str:
    """Return the column used to match an identity on ``rec.table``."""
    if rec.identity_fk_column is not None:
        return rec.identity_fk_column
    # Identity-root table — the PK is the identity reference.
    if rec.table == "identities":
        return "id"
    # Fallback convention: every other table referring to identities
    # does so via ``identity_id``. If a registration wants something
    # different it must set ``identity_fk_column`` explicitly.
    return "identity_id"


_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _validate_sql_identifier(name: str, kind: str) -> str:
    """Reject anything that isn't a plain SQL identifier.

    ``table``, ``fk_column``, and element-values of ``columns`` flow
    straight into an f-string SQL SELECT (SQLAlchemy ``text()`` can only
    bind VALUES, not identifiers). The registry today is populated from
    code-side constants, but ``register_pii_field(...)`` accepts strings
    at runtime — any path that hands user-controlled names through here
    would become a SQL-injection vector. This guard closes that door.
    """
    if not isinstance(name, str) or not _SQL_IDENT_RE.match(name):
        raise ValueError(
            f"pii_registry: refusing to interpolate non-identifier "
            f"{kind}={name!r} into SQL"
        )
    return name


def _fetch_column_rows(
    db: Any,
    table: str,
    fk_column: str,
    identity_id: str,
    columns: Iterable[str],
    *,
    limit: int | None = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Fetch rows from ``table`` where ``fk_column = :identity_id``.

    Uses a plain SQL text query — the registry is name-indexed so we
    cannot reach for the ORM class. Missing tables are treated as an
    empty result set (rather than raising) because not every deployment
    has every registered table enabled.

    Identifier inputs (``table``, ``fk_column``, each of ``columns``)
    are validated by ``_validate_sql_identifier`` BEFORE f-string
    interpolation. ``identity_id`` is always a bound parameter.

    0.9-s A1: soft-delete filter. If the target table has a
    ``deleted_at`` column, we append ``AND deleted_at IS NULL`` so
    logically-deleted rows don't leak into SAR / erasure sweeps.
    Detection is best-effort via information_schema; tables without
    the column are queried unchanged.

    0.9-s A8: pagination. ``limit`` + ``offset`` let callers stream
    large result sets in chunks instead of loading everything into
    memory at once.
    """
    from sqlalchemy import text  # lazy import
    from sqlalchemy.exc import OperationalError, ProgrammingError  # lazy

    safe_table = _validate_sql_identifier(table, "table")
    safe_fk = _validate_sql_identifier(fk_column, "fk_column")
    safe_cols = [_validate_sql_identifier(c, "column") for c in columns]
    col_list = ", ".join(safe_cols)

    # Probe for deleted_at column via a zero-row SELECT — works on both
    # PG and SQLite (information_schema is PG-only). A column-existence
    # error surfaces as OperationalError/ProgrammingError; any other
    # error is swallowed conservatively (skip soft-delete filter rather
    # than mask the whole table).
    has_deleted_at = False
    try:
        db.execute(text(f"SELECT deleted_at FROM {safe_table} WHERE 1=0"))
        has_deleted_at = True
    except (OperationalError, ProgrammingError):
        has_deleted_at = False
    except Exception:  # noqa: BLE001
        has_deleted_at = False

    soft_delete_clause = " AND deleted_at IS NULL" if has_deleted_at else ""
    pagination = ""
    bind: dict[str, Any] = {"iid": identity_id}
    if limit is not None:
        pagination += " LIMIT :lim"
        bind["lim"] = int(limit)
    if offset:
        pagination += " OFFSET :off"
        bind["off"] = int(offset)

    stmt = text(
        f"SELECT {col_list} FROM {safe_table} "
        f"WHERE {safe_fk} = :iid{soft_delete_clause} "
        f"ORDER BY {safe_fk}{pagination}"
    )
    try:
        result = db.execute(stmt, bind)
    except (OperationalError, ProgrammingError) as exc:
        logger.warning(
            "pii_registry: table %s not queryable (%s); treating as empty",
            safe_table, type(exc).__name__,
        )
        return []
    rows: List[Dict[str, Any]] = []
    for row in result:
        rows.append(dict(row._mapping))
    return rows


# 0.9-s A8: per-table row fetch cap. SAR / erasure walks must not load
# unbounded rows into memory. Default ceiling matches what streaming
# JSON exports can realistically buffer; callers wanting higher can
# pass ``per_table_limit`` explicitly.
_DEFAULT_PER_TABLE_ROW_LIMIT = 10_000


def get_pii_for_identity(
    db: Any,
    identity_id: str,
    *,
    per_table_limit: int | None = _DEFAULT_PER_TABLE_ROW_LIMIT,
) -> List[Dict[str, Any]]:
    """Return every registered PII value that references ``identity_id``.

    Each returned dict has:
        - ``table``, ``column``, ``pii_category``, ``retention_days``
        - ``value`` — the current value (may be ``None``)
        - ``row_id`` — the primary key value from the referenced row, if
          the row has a plain ``id`` column

    This is the single source of truth for SAR builds. The caller is
    responsible for authorization + security-definer role context.

    0.9-s A1: soft-deleted rows are filtered out at the ``_fetch_column_rows``
    layer when the table carries ``deleted_at`` (compliance: SAR/erasure
    should not surface tombstoned identities).

    0.9-s A8: per-table row cap (default 10k) bounds memory per SAR.
    If a table returns exactly ``per_table_limit`` rows the result is
    truncated — caller should surface "results truncated" in the export.
    """
    # Group registrations by table so we fetch each table once.
    by_table: Dict[str, List[PIIField]] = {}
    for rec in list_pii_fields():
        by_table.setdefault(rec.table, []).append(rec)

    out: List[Dict[str, Any]] = []
    for table, recs in by_table.items():
        # All registrations for a table must agree on identity_fk_column.
        fk_cols = {_identity_column_for(r) for r in recs}
        if len(fk_cols) != 1:
            raise ValueError(
                f"inconsistent identity_fk_column on table {table!r}: {fk_cols}"
            )
        fk_col = next(iter(fk_cols))
        select_cols = sorted({r.column for r in recs} | {"id"} | {fk_col})
        rows = _fetch_column_rows(
            db, table, fk_col, identity_id, select_cols,
            limit=per_table_limit,
        )
        if per_table_limit is not None and len(rows) >= per_table_limit:
            logger.warning(
                "pii_registry: get_pii_for_identity hit per_table_limit=%d "
                "on table=%s identity_id=%s — results truncated; caller "
                "should paginate via _fetch_column_rows for full export",
                per_table_limit, table, identity_id,
            )
        for row in rows:
            for rec in recs:
                out.append({
                    "table": rec.table,
                    "column": rec.column,
                    "pii_category": rec.pii_category,
                    "retention_days": rec.retention_days,
                    "value": row.get(rec.column),
                    "row_id": row.get("id"),
                    "scrub_strategy": rec.scrub_strategy,
                })
    return sorted(out, key=lambda d: (d["table"], d["column"], str(d.get("row_id"))))
