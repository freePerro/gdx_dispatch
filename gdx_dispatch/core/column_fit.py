"""Clamp extracted text to the column it lands in, reading the limit from the ORM.

Text lifted off a document — by a regex, or by an LLM whose output length
nothing constrains — lands in `String(N)` columns. On Postgres an overlong
value is a hard `StringDataRightTruncation`, which aborts the whole ingest;
SQLite silently accepts it, so the test suite is blind to the failure by
default. That asymmetry is why this belongs at the write, once, rather than
being remembered at each call site.

Issue #513: the vendor-bill parser's terms regex was
``TERMS:\\s*([^.]+)\\.``, and ``[^.]`` is a negated class that matches
newlines — so it ran from "TERMS:" past every line break to the next literal
period in the document, capturing 215 characters into a `String(60)`. The
ingest isolates a failing message per-message and leaves it un-checkpointed, so
that one bill failed on every sweep, forever.

The limit is READ from the column, never restated. The code this replaced
clamped with a literal ``[:500]`` that duplicated a declared length — a second
copy of a number, free to drift from the schema with nothing to notice.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import inspect as sa_inspect


class UnknownColumn(LookupError):
    """`field` is not a mapped attribute on `model`."""


def column_limit(model: type, field: str) -> int | None:
    """The declared length of a String column, or None if it has no limit.

    RAISES on an unknown field rather than returning None. A guard that
    silently declines to guard is worse than no guard: a typo, a rename, or a
    ``mapped_column("db_name")`` alias would otherwise yield no clamp, no
    truncation flag and no error — the exact "green ratchet that cannot fail
    for your defect" shape, moved into a runtime guard.

    Resolution goes through the mapper, keyed by ATTRIBUTE name, because every
    caller passes the attribute; ``Table.c`` is keyed by column name and the
    two differ the moment anyone aliases one.
    """
    try:
        columns = sa_inspect(model).columns
    except Exception as exc:  # not a mapped class at all
        raise UnknownColumn(f"{model!r} is not a mapped class") from exc
    try:
        col = columns[field]
    except KeyError as exc:
        raise UnknownColumn(
            f"{getattr(model, '__name__', model)!r} has no mapped attribute "
            f"{field!r} — clamping it would silently do nothing"
        ) from exc
    return getattr(getattr(col, "type", None), "length", None)


def fit(model: type, field: str, value: Any) -> tuple[Any, bool]:
    """Clamp `value` to its column's declared length.

    Returns ``(value, was_truncated)``. Non-strings and values that already fit
    pass through untouched, so this is safe to apply uniformly across a row's
    fields without knowing which are text.
    """
    if not isinstance(value, str):
        return value, False
    limit = column_limit(model, field)
    if limit is None or len(value) <= limit:
        return value, False
    return value[:limit], True


def fit_all(model: type, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Clamp a whole field mapping. Returns (clamped, names_that_were_cut).

    The caller is expected to record the names somewhere a human will see —
    a truncated value is a fact about the record, not something to swallow.
    """
    out: dict[str, Any] = {}
    cut: list[str] = []
    for name, value in values.items():
        out[name], was = fit(model, name, value)
        if was:
            cut.append(name)
    return out, cut
