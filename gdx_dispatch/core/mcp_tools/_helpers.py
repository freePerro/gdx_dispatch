"""Shared helpers for MCP tool handlers."""
from __future__ import annotations

from typing import Any
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


def agent_visible_message(db: Any, mid: UUID) -> Any:
    """Load one ``OutlookMessage`` an agent principal is allowed to see, or None.

    The load and the privacy gate together, because every email tool needs both
    and needs them in that order. Extracted under GDXA-159: containing the two
    reads made the block long enough that copying it into a third tool tripped
    ``duplicate_block_scan`` with five net-new groups — which is the scanner
    doing its job, so this is the extraction rather than a baseline entry.

    Both reads are ``contained_read``-wrapped. ``db`` is the MCP invoker's
    session and ``routers/ai.py`` reuses it for every tool in an agent turn, so
    an uncontained failure here did not merely fail this tool: over twenty
    ``mcp_tools/*`` modules call ``db.commit()`` on that same session, and
    measured on PG 16.14 a broken ``outlook_settings`` left a LATER tool's
    commit raising ``InFailedSqlTransaction`` with 0 rows persisted.

    ``contained_read`` re-raises, so a genuinely broken read still fails the
    tool exactly as before — only the invoker's transaction survives. Rule 5
    holds because neither read swallows its own failure: ``visible_to_agent``
    has no ``except``, and ``_load_rules`` fails loud by design so a corrupt
    rules row cannot silently un-privatize a mailbox.

    Returns None both for "no such message" and for "hidden from agents", which
    is deliberate and pre-existing: callers answer both with the same "message
    not found", so a machine caller cannot probe whether a hidden id exists.
    """
    from gdx_dispatch.core.database import contained_read
    from gdx_dispatch.modules.outlook.models import OutlookMessage
    from gdx_dispatch.modules.outlook.visibility import visible_to_agent

    with contained_read(db):
        msg = db.get(OutlookMessage, mid)
    if msg is None:
        return None
    with contained_read(db):
        agent_may_see = visible_to_agent(msg, db)
    return msg if agent_may_see else None
