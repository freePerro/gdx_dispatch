"""Per-tenant catalog policy (UX audit F-74 / 2026-04-29).

Three toggles:
  - catalog_require_description       Block catalog item create/update
                                       when description is empty (422).
  - catalog_render_name_when_desc_empty
                                       At render time (lists, invoice
                                       lines), substitute item.name when
                                       description is empty. Default ON
                                       — zero-cost cosmetic fix.
  - catalog_ai_suggest_descriptions   Show "Suggest description (AI)"
                                       button in the catalog editor.

Per Doug 2026-04-29: "lets make it all options for the tenant. I am
assuming most tenants will import using ai. for gdx it will get
fixed as we go."
"""
from gdx_dispatch.modules.catalog_policy.service import (  # noqa: F401
    block_or_warn_invoice_line,
    enforce_save_pricing,
    get_policy,
    require_description_or_422,
)
