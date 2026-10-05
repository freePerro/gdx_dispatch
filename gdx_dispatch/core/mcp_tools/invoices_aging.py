from __future__ import annotations

from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.core.pay_periods import shop_today_from_settings
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

    # The receivable predicate of the collections aging report
    # (routers/collections.py::aging_report): not deleted, not draft, not
    # void, money still owed. The old form called `db.select`, which a
    # Session does not have, and filtered `Invoice.deleted_at is None`, a
    # Python False rather than SQL; it also read `amount_due`, which Invoice
    # does not have (the remainder is `balance_due`). It only ever ran
    # against the unit test's mock (GDXA-209).
    stmt = select(Invoice).where(
        Invoice.deleted_at.is_(None),
        Invoice.status.notin_(("draft", "void")),
        Invoice.balance_due > 0,
    )
    rows = db.execute(stmt).scalars().all()

    # Shop day, the calendar invoice due dates are written in (#444).
    today = shop_today_from_settings(db)

    # Initialize buckets
    buckets = {
        "0-30": {"bucket": "0-30", "count": 0, "total_due": 0.0},
        "31-60": {"bucket": "31-60", "count": 0, "total_due": 0.0},
        "61-90": {"bucket": "61-90", "count": 0, "total_due": 0.0},
        "90+": {"bucket": "90+", "count": 0, "total_due": 0.0},
    }

    for row in rows:
        if not row.due_date:
            continue
        days_past_due = (today - row.due_date).days

        if days_past_due <= 30:
            b_key = "0-30"
        elif days_past_due <= 60:
            b_key = "31-60"
        elif days_past_due <= 90:
            b_key = "61-90"
        else:
            b_key = "90+"

        buckets[b_key]["count"] += 1
        buckets[b_key]["total_due"] += float(row.balance_due)

    return {"summary": list(buckets.values())}


register_tool(DESCRIPTOR, handler)
