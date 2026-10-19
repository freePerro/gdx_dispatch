from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import acting_user_id, coerce_uuid
from gdx_dispatch.models.tenant_models import Customer

DESCRIPTOR = ToolDescriptor(
    name="customers.mark_contacted",
    description=(
        "Records that a customer was contacted (a customer_contacted audit row: "
        "who and when) and optionally appends a note to the customer's notes."
    ),
    blast_radius="green",
    approval_required=False,
    sensitivity_class="internal",
    capabilities_required=[("write", "customer.contact")],
    input_schema={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["customer_id"],
        "additionalProperties": False,
    },
)


async def handler(
    db: Any,
    principal: Any,
    customer_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a contact with the customer and append ``note`` if given.

    The contact itself is the ``customer_contacted`` audit row. Until
    GDXA-1849 this set ``last_contacted_at``, which is not a column on
    ``Customer``, so a call with no note committed nothing, wrote no trail
    and still returned ``{"ok": True}``.
    """
    cid = coerce_uuid(customer_id)
    customer = db.get(Customer, cid) if cid else None
    if customer is None or customer.deleted_at is not None:
        # invoke_tool turns the raise into execution_error, so nothing is committed.
        raise ValueError(f"Customer with id {customer_id} not found.")

    contacted_at = datetime.now(timezone.utc)
    if note:
        current_notes = customer.notes_appended or ""
        customer.notes_appended = f"{current_notes}\n\n{note}" if current_notes else note

    # Staged into the same commit as the note, so neither lands without the other.
    # Not money, so an MCP bearer may do it; its token handle stands in for a user.
    identity = getattr(principal, "identity_id", None)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=acting_user_id(principal) or (str(identity) if identity else None),
        action="customer_contacted",
        entity_type="customer",
        entity_id=str(customer.id),
        details={"note_appended": bool(note), "via": "mcp"},
    )
    db.commit()

    return {
        "ok": True,
        "customer_id": str(customer.id),
        "contacted_at": contacted_at.isoformat(),
        "note_appended": bool(note),
    }


register_tool(DESCRIPTOR, handler)
