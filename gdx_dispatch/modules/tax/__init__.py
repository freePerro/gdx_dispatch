"""Sales tax module.

Sales tax is a deceptively deep topic. A "default rate" works for a
single-jurisdiction GDX-style business but breaks the moment a tenant:
- sells across state lines (nexus rules)
- has tax-exempt customers (resellers, non-profits, government)
- sells category-specific items (groceries / clothing / digital are
  different in many states)
- needs jurisdictional reporting (state + county + city + special
  district stack-ups)
- handles tax holidays, energy-star exemptions, etc.

This module starts with the simple model — one default rate per tenant
— and ships the schema scaffolding for the harder cases so we don't
have to refactor when they land.

**Today (Phase 1):**
- `tax_config` per-tenant: default_rate, name, computed_at
- `tax_exemption` per-customer: exempt boolean, reason, certificate_id

**Future phases (not yet wired):**
- `tax_jurisdiction`: zip → rate lookup table
- `tax_rate_history`: effective_from/effective_to so historical invoices
  stay frozen at the rate that was active when they were issued
- `tax_category`: per-line-item category → rate override
- Avalara / TaxJar integration as a TaxProvider plugin

Per Doug 2026-04-29: "sales tax is a huge nightmare so make it a
seperate module that we can build on."
"""
from gdx_dispatch.modules.tax.router import router  # noqa: F401
