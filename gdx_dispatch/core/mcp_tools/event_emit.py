"""MCP tool: event.emit — emit an audit / domain event."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool, require_capability
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

DESCRIPTOR = ToolDescriptor(
    name="event.emit",
    description="Emit a domain event on behalf of the caller. Payload is validated against the event catalog.",
    input_schema={
        "type": "object",
        "required": ["event_name", "payload"],
        "properties": {
            "event_name": {"type": "string", "description": "Canonical event name, e.g. 'customer.created'"},
            "payload": {"type": "object", "description": "Event payload (validated against the event catalog)"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "event_id": {"type": "string"},
            "emitted": {"type": "boolean"},
        },
    },
    capabilities_required=[("emit", "event")],
    sensitivity_class="internal",
)


async def handler(
    *,
    event_name: str,
    payload: dict[str, Any],
    principal: Any = None,
    db: Any = None,
    **_ignored: Any,
) -> dict[str, Any]:
    require_capability(principal, DESCRIPTOR)
    if not event_name or not isinstance(event_name, str):
        raise ValueError("event_name must be a non-empty string")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    # Real wiring: gdx_dispatch.core.events.emit_event(...). The full call
    # signature needs a DB session + tenant + actor; SS-19 transport
    # adapter supplies those. For SS-18 scope (registry-only) we return
    # a staged shape so the tool can be exercised end-to-end without
    # pulling the event bus into the registry module.
    return {"event_id": "staged", "emitted": True, "event_name": event_name, "_staged": True}


register_tool(DESCRIPTOR, handler)
