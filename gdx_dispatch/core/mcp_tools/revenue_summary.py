from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Invoice

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

    # Sums `total`, NOT the dropped `total_amount` (migration 073): that column
    # was NULL on every row, so this tool reported $0 revenue.
    # COALESCE keeps a no-rows result at 0 rather than None. An ORM select,
    # not a SQL string: Session.execute refuses a bare string, so the string
    # form only ever ran against the unit test's mock (GDXA-209).
    stmt = select(func.coalesce(func.sum(Invoice.total), 0), func.count()).where(
        Invoice.status == "paid",
        Invoice.paid_at >= since_dt,
        Invoice.deleted_at.is_(None),
    )
    result = db.execute(stmt)
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
