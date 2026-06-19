"""Pagination helper for list endpoints.

Standard query params:
  - ``offset`` (int, default 0, >= 0)
  - ``limit`` (int, default 50, 1 <= limit <= MAX_LIMIT=500)

``paginate(query, page)`` applies ``.offset(offset).limit(limit)`` to
an ORM query and runs a ``COUNT`` for the unpaged query. ``PageMeta``
namedtuple returns ``(offset, limit, total, has_more)`` so callers can
emit a consistent response envelope.

``PageParams`` is a Pydantic model suitable for FastAPI dependency
injection (``page: PageParams = Depends()``).

Context: red-team Patterns 6 + 7 — several list endpoints returned
unbounded result sets and/or ran queries without explicit ORDER BY.
SQLAlchemy does not guarantee row order without ORDER BY (cross-DB
portability + pagination-correctness risk), and unbounded lists are
a DoS vector. This module is the shared shim.
"""
from __future__ import annotations

from collections import namedtuple
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Query, Session

MAX_LIMIT: int = 500
DEFAULT_LIMIT: int = 50


PageMeta = namedtuple("PageMeta", ["offset", "limit", "total", "has_more"])


class PageParams(BaseModel):
    """Standard paging params for list endpoints.

    Use as a FastAPI dependency:

        @router.get("/items")
        def list_items(page: PageParams = Depends()): ...
    """

    offset: int = Field(default=0, ge=0, description="Rows to skip.")
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"Max rows to return (capped at {MAX_LIMIT}).",
    )

    def clamped(self) -> "PageParams":
        """Return a copy with limit clamped to [1, MAX_LIMIT].

        Pydantic ``Field(le=MAX_LIMIT)`` already rejects >MAX; this is a
        belt-and-suspenders helper for callers that build PageParams
        programmatically bypassing validation.
        """
        return PageParams(
            offset=max(0, self.offset),
            limit=max(1, min(self.limit, MAX_LIMIT)),
        )


def paginate(
    query: Any,
    page: PageParams,
    *,
    db: Session | None = None,
) -> tuple[Any, PageMeta]:
    """Apply offset/limit to ``query`` and compute a ``PageMeta``.

    Parameters
    ----------
    query:
        Either a legacy ``Query`` (``db.query(Model)...``) or a 2.0-style
        ``select()`` statement. The function detects the shape.
    page:
        ``PageParams`` — offset/limit.
    db:
        Required when ``query`` is a ``select()`` statement, because the
        count has to be executed through a Session. Optional when
        ``query`` is a legacy ``Query`` (it carries its own session).

    Returns
    -------
    (paged_query_or_stmt, PageMeta)
        The caller still executes the first element (``.all()`` or
        ``db.execute(...).scalars().all()``). Total is computed by a
        ``COUNT`` wrapped around the unpaged query.
    """
    p = page.clamped()

    # Legacy Query path: has .count(), .offset(), .limit().
    if isinstance(query, Query):
        total = query.count()
        paged = query.offset(p.offset).limit(p.limit)
        has_more = (p.offset + p.limit) < total
        return paged, PageMeta(
            offset=p.offset, limit=p.limit, total=total, has_more=has_more
        )

    # 2.0-style select() statement path.
    if db is None:
        raise TypeError(
            "paginate(): db=Session is required when query is a select() statement"
        )
    count_stmt = select(func.count()).select_from(query.subquery())
    total = int(db.execute(count_stmt).scalar_one())
    paged = query.offset(p.offset).limit(p.limit)
    has_more = (p.offset + p.limit) < total
    return paged, PageMeta(
        offset=p.offset, limit=p.limit, total=total, has_more=has_more
    )


def envelope(items: list[Any], meta: PageMeta) -> dict[str, Any]:
    """Standard ``{items, meta}`` response envelope."""
    return {
        "items": items,
        "meta": {
            "offset": meta.offset,
            "limit": meta.limit,
            "total": meta.total,
            "has_more": meta.has_more,
        },
    }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "PageMeta",
    "PageParams",
    "envelope",
    "paginate",
]
