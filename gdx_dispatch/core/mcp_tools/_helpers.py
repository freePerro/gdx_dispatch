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


def acting_user_id(principal: Any) -> str | None:
    """The person a tool's write is recorded against, or None if there is none.

    A human principal (the login session) acts as itself. An ``ai_worker``
    acts for ``delegated_by_user_id``, the person whose ``/api/ai/ask`` turn
    minted it (``routers/ai.py``). An MCP bearer is an ``ai_worker`` too, but
    its ``identity_id`` is a UUID5 of the token (``mcp_fastmcp_bridge``) and
    names nobody, so it gets None and a money tool refuses it.
    """
    if getattr(principal, "actor_type", "human") == "ai_worker":
        delegated = getattr(principal, "delegated_by_user_id", None)
        return str(delegated) if delegated else None
    identity = getattr(principal, "identity_id", None)
    return str(identity) if identity else None


def estimate_line_dict(
    line: Any,
    *,
    with_id: bool = True,
    with_estimate_id: bool = False,
    with_sort_order: bool = False,
) -> dict[str, Any]:
    """Serialize one ``EstimateLine`` row the way the estimate tools return it.

    One shape for every tool, so the money fields cannot drift apart:
    ``quantity`` is an int, ``unit_price`` and ``line_total`` are floats.
    Extracted under GDXA-1683 from four hand-written copies in
    ``estimates.get``, ``estimates.update_line`` (preview ``before`` and the
    commit result) and ``estimates.add_line`` (commit result); one of them
    returned ``quantity`` raw instead of ``int(...)``.

    The flags add or drop keys without moving the others, so each caller's
    key order is the one it had before: ``id``, ``estimate_id``,
    ``description``, ``quantity``, ``unit_price``, ``line_total``,
    ``sort_order``. Pinned per tool by ``tests/test_tool_estimates_line_shape.py``.
    """
    out: dict[str, Any] = {}
    if with_id:
        out["id"] = str(line.id)
    if with_estimate_id:
        out["estimate_id"] = str(line.estimate_id)
    out["description"] = line.description
    out["quantity"] = int(line.quantity)
    out["unit_price"] = float(line.unit_price)
    out["line_total"] = float(line.line_total)
    if with_sort_order:
        out["sort_order"] = line.sort_order
    return out


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
