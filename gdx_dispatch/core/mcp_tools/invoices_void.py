from __future__ import annotations

from typing import Any

from gdx_dispatch.core.invoice_void import InvoiceVoidRefused, void_invoice
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.core.mcp_tools._helpers import acting_user_id, coerce_uuid
from gdx_dispatch.models.tenant_models import Invoice

DESCRIPTOR = ToolDescriptor(
    name="invoices.void",
    description="Void an invoice. Red — admin capability required regardless of approval_ref.",
    blast_radius="red",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "invoice")],
    input_schema={
        "type": "object",
        "required": ["invoice_id"],
        "properties": {
            "invoice_id": {"type": "string"},
            "reason": {"type": "string"},
            "approval_ref": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "preview": {
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string"},
                    "invoice_number": {"type": "string"},
                    "status": {"type": "string"},
                    "total": {"type": "number"},
                    "balance_due": {"type": "number"},
                },
            },
            "voided": {
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string"},
                    "invoice_number": {"type": "string"},
                    "previous_status": {"type": "string"},
                    "status": {"type": "string"},
                    "balance_due": {"type": "number"},
                    "reason": {"type": "string", "nullable": True},
                    "released_parts": {"type": "integer"},
                    "released_change_orders": {"type": "integer"},
                    "released_time_entries": {"type": "integer"},
                },
            },
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    invoice_id: str,
    reason: str | None = None,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Void an invoice through the one shared void (``core/invoice_void.py``).

    The admin gate and the approval_ref check are ``invoke_tool``'s, for every
    red tool. The status change, ledger reversal, releases, ``invoice_voided``
    audit row and stale-intent sweep are all the shared function's. Until
    GDXA-1849 this handler only read the invoice and returned this success
    shape, so nothing was voided (GDXA-1847). Every refusal raises, so a
    refusal never comes back looking like a void (on the staging call
    ``invoke_tool`` logs the raise and still returns approval_required).

    ``invoke_tool`` also calls the handler on the unapproved first call, to
    stage the pending action, and passes ``approval_ref`` only on the approved
    one. Without it this returns a read-only ``preview`` and writes nothing.
    """
    actor = acting_user_id(principal)
    if actor is None:
        raise PermissionError(
            "invoices.void needs the acting person: this caller names no user, "
            "and a money mutation is never anonymous"
        )
    iid = coerce_uuid(invoice_id)
    inv = db.get(Invoice, iid) if iid else None
    if not approval_ref:
        if inv is None or inv.deleted_at is not None:
            raise InvoiceVoidRefused(404, "Invoice not found")
        if inv.status == "void":
            raise InvoiceVoidRefused(409, "invoice is already void")
        return {
            "preview": {
                "invoice_id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "status": inv.status,
                "total": float(inv.total or 0),
                "balance_due": float(inv.balance_due or 0),
            }
        }
    previous_status = inv.status if inv is not None else None
    result = void_invoice(db, invoice_id, actor=actor, reason=reason)
    if result.already_void:
        # The shared void returns an already-void invoice untouched. Reporting
        # that as {"voided": ...} would claim a void this call did not do.
        raise InvoiceVoidRefused(409, "invoice is already void")
    row = result.invoice
    return {
        "voided": {
            "invoice_id": str(row.id),
            "invoice_number": row.invoice_number,
            "previous_status": previous_status,
            "status": row.status,
            "balance_due": float(row.balance_due or 0),
            "reason": reason,
            "released_parts": result.released_parts,
            "released_change_orders": result.released_change_orders,
            "released_time_entries": result.released_time_entries,
        }
    }


register_tool(DESCRIPTOR, handler)
