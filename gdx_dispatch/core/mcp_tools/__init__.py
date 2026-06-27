"""SS-18 slice D — initial MCP tool set.

Importing this package registers the baseline tools with
``gdx_dispatch.core.mcp_registry``. Each submodule defines a descriptor +
handler pair and calls ``register_tool(descriptor, handler)`` at
import time.

Tools shipped in the alpha set
------------------------------
* ``pat.rotate``       — admin:pat, restricted, approval_required
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
    get_customer_detail,
    get_job_detail,
    invoices_aging,
    invoices_create_draft,
    invoices_void,
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
