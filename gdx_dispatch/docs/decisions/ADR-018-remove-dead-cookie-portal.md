# ADR-018 — Remove the dead cookie-based customer-portal module

**Status:** Accepted (Doug, 2026-07-01). Executed in the same change that records it — the code
was already unreachable, so there is no migration window.

## Context

The customer portal grew two parallel backend implementations:

1. **`gdx_dispatch/routers/portal.py`** — JWT-based magic-link flow (`POST /portal/login` →
   `GET /portal/verify` → Bearer token). This is the one registered in `app.py`, the one the Vue
   `CustomerPortalView` talks to, and the one finished end-to-end on 2026-07-01 (estimates
   accept/decline, staff `/api/portal` management, transactional-email delivery).
2. **`gdx_dispatch/modules/customer_portal/router.py` + `auth.py`** — an older cookie-based flow
   (`customer_portal_user_id` httponly cookie, `/portal/auth/magic-link`,
   `/portal/auth/verify/{token}`, a Stripe Checkout redirect pay path, and a tokenized
   `public_invoice` route).

The 2026-07-01 adversarial audit of the portal-finish change established the decisive fact:
**the module router is imported (via the package `__init__`) but never `include_router`ed
anywhere** — every one of its routes is unreachable and has been for as long as the current
`app.py` registration list has existed. It is not a fallback, not a shadow, not a compat layer;
it is 114 lines of dead code that *looks* load-bearing. It already cost one debugging loop: an
earlier repo survey concluded "two parallel portal auth systems are both mounted" and flagged
route-precedence risk that did not exist.

Dead-but-plausible code in an auth surface is a real hazard here, not just clutter:

- It contains an **unauthenticated-cookie auth pattern** (raw user-id cookie, no signing) that
  must never be revived by copy-paste.
- Its `send_magic_link` returns the magic link in the HTTP response body — an enumeration +
  token-leak pattern the live router deliberately avoids.
- Its pay path creates Stripe Checkout sessions against `invoice.total` (ignores
  `balance_due`), diverging from the live PaymentIntent path.

## Decision

Delete the cookie-based implementation; keep the model.

- **Delete** `gdx_dispatch/modules/customer_portal/router.py` (cookie routes, checkout pay,
  `public_invoice`).
- **Delete** `gdx_dispatch/modules/customer_portal/auth.py` (`send_magic_link`,
  `verify_magic_link` — superseded by the token handling inside `routers/portal.py`).
- **Keep** `gdx_dispatch/modules/customer_portal/models.py` (`CustomerUser`) — it is the live
  credential store for the JWT router; `__init__.py` shrinks to exporting `models` only.
- **Update** `tests/test_05_sprint4_compliance.py`: `test_magic_link_verify` exercised the dead
  helpers; the equivalent behavior (issue → verify → single-use) is already covered against the
  live router by `tests/test_customer_portal.py` (`test_verify_valid_token_returns_jwt`,
  `test_verify_token_cannot_be_reused`, `test_admin_invite_creates_user_and_link`), so the dead
  test is removed rather than ported.

## Amendment (same day, second audit round)

The claim "the module boundary contained the cookie pattern" was **wrong by one file**:
`gdx_dispatch/routers/payments.py` — which IS mounted at `/payments` — authenticated its Stripe
endpoints (`/payments/intent`, `/payments/charge`, ACH setup) with the same raw
`customer_portal_user_id` cookie. Since the deleted route was the only thing that ever *set*
that cookie, those endpoints were reachable by anyone forging a cookie containing a valid
`CustomerUser` UUID (mitigated in practice only by `CustomerUser.stripe_customer_id` not
existing as a column, so every request 400'd after auth). Fixed in the same change:
`payments._current_portal_user` now depends on the portal JWT
(`routers/portal.get_current_portal_customer`). A repo-wide grep for
`customer_portal_user_id` now returns zero hits outside this ADR.

## Consequences

- `Invoice.public_token` remains in the schema (other surfaces and historical links may use it);
  only the module's unreachable *route* over it goes away. If a public tokenized invoice view is
  wanted later, it should be added to `routers/portal.py` deliberately, with the expiry rules in
  one place.
- No API surface changes: nothing served by the app before differs after (verified by diffing the
  registered-route list pre/post removal).
- The `customer_portal` *module key* (grants, gating) is unaffected — it gates the live router.

## Rejected alternatives

- **Mount it as a compat layer** — rejected: nothing ever linked customers to its URL shapes, so
  there is nothing to be compatible with.
- **Keep it as reference** — rejected: git history preserves it; unreachable auth code invites
  accidental revival and misleads future audits (already happened once, see Context).
- **Port its `public_invoice` route to the live router now** — rejected as scope creep: no
  current consumer; the Stripe-redirect return target it served is itself part of the dead path.
