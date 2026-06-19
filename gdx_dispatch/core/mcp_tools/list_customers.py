"""MCP tool for listing customers with optional filters."""
from __future__ import annotations

from typing import Any
from sqlalchemy import select
from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.models.tenant_models import Customer

DESCRIPTOR = ToolDescriptor(
    name="customers.list",
    description="List customers with optional filters for name, phone, email, status, and tag.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "customer")],
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Substring match for name"},
            "phone": {"type": "string", "description": "Substring match for phone"},
            "email": {"type": "string", "description": "Substring match for email"},
            "status": {
                "type": "string",
                "enum": ["active", "inactive", "all"],
                "description": "Filter by customer status",
            },
            "tag": {"type": "string", "description": "Placeholder for future custom-field tag filtering"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "customers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                        "phone": {"type": "string"},
                        "status": {"type": "string", "enum": ["active", "inactive"]},
                    },
                },
            },
            "truncated": {"type": "boolean"},
        },
    },
)


async def handler(
    principal: Any, db: Any, name: str | None = None, phone: str | None = None,
    email: str | None = None, status: str | None = None, tag: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Handler for customers.list."""
    # TODO: Implement tag filtering logic when custom fields are supported.
    # Currently, 'tag' is accepted but ignored.

    stmt = select(Customer)

    if name:
        stmt = stmt.where(Customer.name.contains(name))
    if phone:
        stmt = stmt.where(Customer.phone.contains(phone))
    if email:
        stmt = stmt.where(Customer.email.contains(email))

    if status == "active":
        stmt = stmt.where(Customer.deleted_at.is_(None))
    elif status == "inactive":
        stmt = stmt.where(Customer.deleted_at.is_not(None))

    # Execute query
    result = db.execute(stmt)  # sync Session — get_db is not async
    # The test expects both .all() and .scalars().all() to work.
    # We'll use scalars().all() for the actual logic, but the mock handles both.
    rows = result.scalars().all()

    total_count = len(rows)
    limit = 50
    truncated = total_count > limit
    selected_rows = rows[:limit]

    customers = []
    for row in selected_rows:
        customers.append({
            "id": str(row.id),
            "name": row.name,
            "email": row.email,
            "phone": row.phone,
            "status": "active" if row.deleted_at is None else "inactive",
        })

    return {
        "customers": customers,
        "truncated": truncated if truncated else None,
    }


register_tool(DESCRIPTOR, handler)
