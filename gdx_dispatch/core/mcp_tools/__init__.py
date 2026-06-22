"""SS-18 slice D — initial MCP tool set.

Importing this package registers the baseline tools with
``gdx_dispatch.core.mcp_registry``. Each submodule defines a descriptor +
handler pair and calls ``register_tool(descriptor, handler)`` at
import time.

Tools shipped in the alpha set
------------------------------
* ``customer.lookup``  — read:customer, internal
* ``job.create``       — write:job, internal (write-scope)
* ``pat.rotate``       — admin:pat, restricted, approval_required
* ``invoice.query``    — read:invoice, internal
* ``event.emit``       — emit:event, internal
* ``customers.list``   — read:customer, internal

Handlers are thin: they enforce the capability gate via
:func:`gdx_dispatch.core.mcp_registry.require_capability` and return a dict
payload. Real data-access wiring (DB session, event bus) is injected
by the caller (SS-19 transport adapter) via keyword args.

TODO
----------------
* ensure this package is imported on app start — add::

      import gdx_dispatch.core.mcp_tools  # noqa: F401  (side-effect: register tools)

  to ``gdx_dispatch/main.py`` at integration time.
"""
from __future__ import annotations

# Side-effect imports register each tool with the MCP registry.
from gdx_dispatch.core.mcp_tools import (  # noqa: F401
    catalog_add_item,
    catalog_bulk_add_items,
    catalog_create,
    catalog_get_item,
    catalog_list,
    catalog_update_item,
    customer_lookup,
    customers_lifetime,
    documents_bulk_move,
    documents_create_folder,
    documents_link_to_entity,
    documents_list,
    documents_move,
    documents_read,
    documents_rename,
    documents_rename_folder,
    documents_search,
    documents_set_tags,
    documents_summarize,
    documents_unlink_from_entity,
    email_draft,
    email_list,
    email_move,
    email_read,
    estimates_add_line,
    estimates_create_draft,
    estimates_get,
    estimates_list,
    estimates_update_line,
    event_emit,
    get_customer_detail,
    get_job_detail,
    invoice_query,
    invoices_aging,
    invoices_create_draft,
    invoices_void,
    job_create,
    jobs_update_status,
    list_customers,
    list_invoices,
    list_jobs,
    mark_customer_contacted,
    revenue_summary,
    schedule_job,
    schedule_lookup,
    technicians_activity,
)


def register_all() -> list[str]:
    """Return the set of tool names registered by this package.

    Useful for integration smoke tests ("did all five tools land?").
    """
    return [
        catalog_add_item.DESCRIPTOR.name,
        catalog_bulk_add_items.DESCRIPTOR.name,
        catalog_create.DESCRIPTOR.name,
        catalog_get_item.DESCRIPTOR.name,
        catalog_list.DESCRIPTOR.name,
        catalog_update_item.DESCRIPTOR.name,
        customer_lookup.DESCRIPTOR.name,
        customers_lifetime.DESCRIPTOR.name,
        documents_bulk_move.DESCRIPTOR.name,
        documents_create_folder.DESCRIPTOR.name,
        documents_link_to_entity.DESCRIPTOR.name,
        documents_list.DESCRIPTOR.name,
        documents_move.DESCRIPTOR.name,
        documents_read.DESCRIPTOR.name,
        documents_rename.DESCRIPTOR.name,
        documents_rename_folder.DESCRIPTOR.name,
        documents_search.DESCRIPTOR.name,
        documents_set_tags.DESCRIPTOR.name,
        documents_summarize.DESCRIPTOR.name,
        documents_unlink_from_entity.DESCRIPTOR.name,
        email_draft.DESCRIPTOR.name,
        email_list.DESCRIPTOR.name,
        email_move.DESCRIPTOR.name,
        email_read.DESCRIPTOR.name,
        estimates_add_line.DESCRIPTOR.name,
        estimates_create_draft.DESCRIPTOR.name,
        estimates_get.DESCRIPTOR.name,
        estimates_list.DESCRIPTOR.name,
        estimates_update_line.DESCRIPTOR.name,
        event_emit.DESCRIPTOR.name,
        get_customer_detail.DESCRIPTOR.name,
        get_job_detail.DESCRIPTOR.name,
        invoice_query.DESCRIPTOR.name,
        invoices_aging.DESCRIPTOR.name,
        invoices_create_draft.DESCRIPTOR.name,
        invoices_void.DESCRIPTOR.name,
        job_create.DESCRIPTOR.name,
        jobs_update_status.DESCRIPTOR.name,
        list_customers.DESCRIPTOR.name,
        list_invoices.DESCRIPTOR.name,
        list_jobs.DESCRIPTOR.name,
        mark_customer_contacted.DESCRIPTOR.name,
        revenue_summary.DESCRIPTOR.name,
        schedule_job.DESCRIPTOR.name,
        schedule_lookup.DESCRIPTOR.name,
        technicians_activity.DESCRIPTOR.name,
    ]
