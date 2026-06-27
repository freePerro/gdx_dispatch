"""Shared helpers for MCP tool handlers."""
from __future__ import annotations

from uuid import UUID


def coerce_uuid(raw: str | None) -> UUID | None:
    """Return a ``UUID`` for ``raw``, or ``None`` if it isn't a valid UUID.

    Accepts ``None``/empty input (returns ``None``) so callers can pass an
    optional id field straight through without a pre-check. This is the shared
    implementation that previously lived as a per-module ``_coerce_uuid`` copy
    in each tool file.
    """
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
