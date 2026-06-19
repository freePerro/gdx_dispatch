"""Billing terms — per-tenant payment-terms defaults + early-pay /
late-fee / interest configuration (UX audit F-36 / 2026-04-29).

Per Doug 2026-04-29: "c with options for contractor retail and
wholesale the ability to add a discount if paid before x date and
the ability to have late charges and interest if after x date."

Effective payment-terms-days resolution order on invoice creation:
  1. Customer.payment_terms_days (customer-level override)
  2. tenant_settings.{contractor|retail|wholesale}_payment_terms_days
     (per Customer.pricing_class)
  3. tenant_settings.default_payment_terms_days  (default 30)

The discount / late-fee / interest *application* is a follow-up
sprint — the configuration columns + the service.resolve_terms()
helper land today so newly-created invoices stop coming in with
NULL due_date."""
from gdx_dispatch.modules.billing_terms.service import (  # noqa: F401
    EffectiveTerms,
    resolve_effective_terms,
)
