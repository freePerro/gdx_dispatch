from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool


DESCRIPTOR = ToolDescriptor(
    name="revenue.summary",
    description="Sum paid invoice totals over a window (default last 30 days).",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "invoice")],
    input_schema={
        "type": "object",
        "properties": {
            "since": {"type": "string", "format": "date-time"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "revenue": {
                "type": "object",
                "properties": {
                    "total": {"type": "number"},
                    "invoice_count": {"type": "integer"},
                    "window": {"type": "string"},
                },
            },
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    since: str | None = None,
    **_
) -> dict[str, Any]:
    """Sum paid invoice totals over a window."""

    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(days=30)

    # The query uses COALESCE to ensure we get 0 instead of None for the sum.
    # We assume the existence of 'paid_at' and 'status' columns based on the spec.
    query = """
        SELECT COALESCE(SUM(total_amount), 0), COUNT(*)
        FROM invoices
        WHERE status = 'paid'
          AND paid_at >= :since
          AND deleted_at IS NULL
    """

    result = db.execute(query, {"since": since_dt})
    row = result.first()

    # row is expected to be (total, count)
    total = float(row[0]) if row[0] is not None else 0.0
    count = int(row[1]) if row[1] is not None else 0

    return {
        "revenue": {
            "total": total,
            "invoice_count": count,
            "window": f"since {since_dt.isoformat()}",
        }
    }


register_tool(DESCRIPTOR, handler)
