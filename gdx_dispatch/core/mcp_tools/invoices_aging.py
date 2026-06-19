from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Invoice

DESCRIPTOR = ToolDescriptor(
    name="invoices.aging_summary",
    description="Aggregate unpaid invoices into 0-30/31-60/61-90/90+ days-past-due buckets.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "invoice")],
    input_schema={
        "type": "object",
        "properties": {},
    },
    output_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bucket": {"type": "string"},
                        "count": {"type": "integer"},
                        "total_due": {"type": "number"},
                    },
                    "required": ["bucket", "count", "total_due"],
                },
            },
        },
        "required": ["summary"],
    },
)


async def handler(principal: Any, db: Any, **kwargs: Any) -> dict[str, Any]:
    """Aggregate unpaid invoices into aging buckets."""

    # Query unpaid, non-deleted invoices
    # Note: Using standard SQLAlchemy-style filtering as implied by the test mock
    # The test mock uses result.scalars().all()
    stmt = (
        db.execute(
            db.select(Invoice).where(
                Invoice.status != "paid",
                Invoice.deleted_at is None
            )
        )
    )
    rows = stmt.scalars().all()

    today = datetime.now(timezone.utc).date()

    # Initialize buckets
    buckets = {
        "0-30": {"bucket": "0-30", "count": 0, "total_due": 0.0},
        "31-60": {"bucket": "31-60", "count": 0, "total_due": 0.0},
        "61-90": {"bucket": "61-90", "count": 0, "total_due": 0.0},
        "90+": {"bucket": "90+", "count": 0, "total_due": 0.0},
    }

    for row in rows:
        # row is an Invoice instance
        due_date = row.due_date
        # Ensure we are comparing date to date
        if isinstance(due_date, datetime):
            due_date = due_date.date()

        days_past_due = (today - due_date).days

        if days_past_due <= 30:
            b_key = "0-30"
        elif days_past_due <= 60:
            b_key = "31-60"
        elif days_past_due <= 90:
            b_key = "61-90"
        else:
            b_key = "90+"

        buckets[b_key]["count"] += 1
        buckets[b_key]["total_due"] += float(row.amount_due)

    return {"summary": list(buckets.values())}


register_tool(DESCRIPTOR, handler)
