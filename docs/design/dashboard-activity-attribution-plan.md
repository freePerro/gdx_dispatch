# Dashboard "Recent Activity" — who did it, and to what

**Status:** plan, not started
**Filed:** 2026-07-28
**Trigger:** prod dashboard shows rows like `Data Accessed (customer)` with no
customer and no user.

---

## What prod actually shows

Measured against the live tenant DB (`audit_logs`, last 30 days, read-only
query). Auth/session rows are already filtered out of the dashboard feed, so
these are the rows a user actually sees:

| action | entity_type | count |
| --- | --- | --- |
| `patch_line` | `estimate_line` | 1227 |
| `patch_estimate` | `estimate` | 522 |
| `data_accessed` | `customer` | 78 |
| `add_line` | `estimate_line` | 78 |
| `delete_line` | `estimate_line` | 35 |
| `qb_webhook_received` | `qb_webhook` | 21 |
| `estimate_marked_sent` | `estimate` | 21 |
| `notification_deleted` | `notification` | 19 |
| `estimate_created` | `estimate` | 18 |
| `job_updated` | `job` | 18 |

A representative slice of the most recent rows (identifiers redacted — public repo):

```text
data_accessed | customer | <uuid> | {"scope": "single_customer"} | <user-uuid>
data_accessed | customer | <uuid> | {"scope": "single_customer"} | <user-uuid>
job_updated   | job      | <uuid> | {"fields": [...]}            | <user-uuid>
patch_invoice | invoice  | <uuid> | {}                           | system
add_invoice_line | invoice_line | <uuid> | {}                    | system
```

The row carries a UUID for the subject and, most of the time, the literal
string `system` for the actor. Neither is something a human can read.

**Actor attribution, last 30 days (non-auth rows):**

| user_id | rows |
| --- | --- |
| `system` | 1909 |
| (one real user UUID) | 328 |
| (an API-key actor) | 8 |
| (2 other real users) | 6 |

**85% of visible audit rows have no recorded actor at all.** This is not a
display bug — the information was never written.

---

## Three distinct defects

### D1 — The dashboard throws away the actor it already has

`/api/audit/logs` resolves and returns `user_name` for every row
([audit.py:102](../../gdx_dispatch/routers/audit.py#L102), batch-resolved via
`_resolve_user_names` at [audit.py:51](../../gdx_dispatch/routers/audit.py#L51),
falling back `name → full_name → email → short id`).

The dashboard reads the same endpoint and maps only `action`, `entity_type`,
and `created_at`
([DashboardView.vue:754-759](../../gdx_dispatch/frontend/src/views/DashboardView.vue#L754-L759)).
`user_name` is dropped on the floor. `meta` is the timestamp and nothing else
([DashboardView.vue:263](../../gdx_dispatch/frontend/src/views/DashboardView.vue#L263)).

Cheapest fix in the whole plan: one line of the map function. It fixes the
"who" for the 15% of rows that have a real actor, and makes D2's damage
visible for the rest.

### D2 — 54 handlers record `system` instead of the real user

Root cause found. A batch of audit blocks were generated with a defensive
locals-sniffing preamble:

```python
_audit_user_obj = locals().get('user') or locals().get('current_user') or {}
...
_audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
```

— but the enclosing handlers bind the auth dependency to `_`, not `user` or
`current_user`:

```python
def patch_line(
    estimate_id: UUID,
    line_id: UUID,
    payload: EstimateLinePatchIn,
    request: Request,
    _: dict = Depends(get_current_user),   # <-- named `_`
    db: Session = Depends(get_db),
):
```

`locals().get('user')` misses, `locals().get('current_user')` misses, and every
row falls through to `'system'`
([estimates.py:1125-1146](../../gdx_dispatch/routers/estimates.py#L1125-L1146)).

Scanned the whole router tree: **84 generated audit blocks across 17 routers.
30 resolve a real actor; 54 hard-fail to `system`.**

Affected routers: `pricing`, `loyalty`, `vendors`, `mobile`, `maps`,
`change_orders`, `labor`, `collections`, `communications`, `expenses`,
`customers`, `settings`, `payments`, `inventory`, `purchase_orders`,
`estimates`, `invoices`.

This is the actual "who made the change" bug. It also means the audit trail
itself — the compliance artifact, not just the dashboard widget — has no actor
on 85% of write events. That is a bigger deal than the dashboard.

### D3 — The subject is a UUID with no name anywhere

`audit_logs` stores `entity_type` + `entity_id` and nothing else identifying.
`details` is usually `{}` or a field list. There is no code path anywhere that
turns `customer/<uuid>` into a customer name.

So `Data Accessed (customer)` is the literal best the current data supports.
(`data_accessed` isn't in `ACTIVITY_LABELS`, so `formatActivityTitle` falls
through to title-case + entity suffix —
[DashboardView.vue:719-725](../../gdx_dispatch/frontend/src/views/DashboardView.vue#L719-L725).)

### Secondary — signal-to-noise

`estimate_line` patches are 1340 of ~2250 visible rows (~60%). The dashboard
shows 10. Even with perfect names, the widget will read as ten near-identical
"Estimate line updated" entries. Naming without filtering is half a fix.

### Secondary — stale user cache

[activity.py:31](../../gdx_dispatch/routers/activity.py#L31) holds a
module-global `_user_cache: dict[str, str]`, never invalidated and never
bounded. A renamed user shows the old name until the process restarts. Fold it
into the shared resolver.

---

## Plan

### Phase 1 — Show the actor we already have (frontend only, ~30 min)

- `DashboardView.vue` `loadRecentActivity`: carry `evt.user_name` through, and
  build `meta` as `` `${who} · ${when}` ``.
- Reuse `ActivityView.vue`'s `formatUser` guard
  ([ActivityView.vue:166-175](../../gdx_dispatch/frontend/src/views/ActivityView.vue#L166-L175))
  — it already maps `system`/`anonymous` → "System" and short-circuits a raw
  UUID to `Unknown user (abc12345)`. Lift it to
  `composables/useFormatters.js` so both views share one implementation.
- Add `data_accessed: "Customer record viewed"` and the other missing top-10
  actions (`patch_line`, `patch_estimate`, `add_line`, `delete_line`,
  `patch_invoice`, `invoice_marked_sent`, `estimate_marked_sent`,
  `notification_deleted`) to `ACTIVITY_LABELS`.

Ships value immediately and is independently deployable.

### Phase 2 — Name the subject (backend, ~half a day)

Add a batch entity-label resolver in `gdx_dispatch/core/` (new module, e.g.
`audit_labels.py`), used by both `audit.py` and `activity.py`:

```python
def resolve_entity_labels(db, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], str]
```

One grouped query per entity_type, one pass per page of rows — never N+1.
Registry covers the types that actually occur in prod:

| entity_type | label source |
| --- | --- |
| `customer` | `Customer.name` (plaintext today — see the encryption note below) |
| `job` | `Job.job_number` + title |
| `invoice` | `Invoice.invoice_number` |
| `estimate` | `Estimate.estimate_number` (+ `label`) |
| `estimate_line` / `invoice_line` | hop to parent, label as the parent estimate/invoice |
| `user` | reuse `_resolve_user_names` |
| `lead`, `landing_lead`, `notification`, `vendor` | name/subject field |

Unknown types degrade to `null` — the UI falls back to today's text. Wrap the
whole resolver in try/except so the feed never breaks over a display field
(same contract as `_display_state_for_jobs` in
[customers.py:459-467](../../gdx_dispatch/routers/customers.py#L459-L467)).

Emit two new fields per row: `entity_label` and `entity_url` (deep link).

Read-time resolution, not write-time stamping — it retro-labels the existing
78 `data_accessed` rows and the whole history, and it keeps the hash-chained
`details` payload untouched (the table is immutable by trigger;
[audit.py:147-278](../../gdx_dispatch/core/audit.py#L147-L278) — we cannot
backfill `details` even if we wanted to).

**Encryption caveat:** `Customer.name` is plaintext *for now* by explicit
decision (search-architecture decision `D-S122-9`,
[tenant_models.py:128-141](../../gdx_dispatch/models/tenant_models.py#L128-L141)).
If that decision flips to encrypted, this resolver decrypts per row — note it
in the resolver so it isn't a surprise later.

Frontend: render `title` as `<label> — <entity_label>`, make the row a link to
`entity_url`, mirroring the Today's Schedule row
([DashboardView.vue:274](../../gdx_dispatch/frontend/src/views/DashboardView.vue#L274)).

### Phase 3 — Fix the actor at the source (backend, ~half a day, mechanical)

Rename the auth dependency param `_` → `current_user` in the 54 handlers that
carry a generated audit block, so the existing `locals()` lookup resolves.
Better: while touching each one, replace the locals-sniffing preamble with a
direct `current_user` reference — the sniffing is what made this failure silent
and invisible.

Then add the regression gate that would have caught it:

- A test that walks every route in the app, and for each handler containing a
  `log_audit_event*` call, asserts the actor expression can resolve — i.e. the
  handler has a `Depends(get_current_user)` parameter bound to a name the audit
  block actually reads. Fails the build on the next generated block that
  reintroduces the bug.
- A runtime assertion in `_log_audit_event_impl`: if `request.state.user`
  exists but `user_id` came in falsy, log a warning. Catches the class of bug
  rather than the 54 instances.

Do **not** backfill history. `audit_logs` is append-only and trigger-enforced;
the 1909 existing `system` rows stay as they are. Their actor is genuinely
unrecoverable.

### Phase 4 — Make the feed worth reading (decision needed, see below)

Options, not yet chosen:

- Add `patch_line`/`add_line`/`delete_line` on `estimate_line`/`invoice_line`
  to `ACTIVITY_HIDE_ACTIONS` — line edits become visible only on the estimate
  itself, where they belong.
- Or collapse consecutive same-entity events into one row ("Estimate E-1042 —
  6 line edits by Amber, 2:14 PM").
- Move `data_accessed` out of the dashboard feed entirely. It is a GDPR
  read-log, not activity; there is already a dedicated GDPR access log
  (`gdpr_data_access_logs`,
  [core/data_access_logger.py](../../gdx_dispatch/core/data_access_logger.py)).
  Arguably `data_accessed` should never have been in `audit_logs` at all
  ([customers.py:482-492](../../gdx_dispatch/routers/customers.py#L482-L492)).

---

### Phase 5 — Customer-side activity: portal access and email engagement

Requested 2026-07-28. This adds a second *class* of actor to the feed — the
customer, not the staff user — which has consequences for Phase 2's resolver.

#### 5a — Portal activity is already logged; it just renders wrong (small)

`portal.py` already writes audit rows for the events worth seeing:

| action | entity_type | source |
| --- | --- | --- |
| `portal_login_verified` (magic link) | `customer_user` | [portal.py:320](../../gdx_dispatch/routers/portal.py#L320) |
| `portal_password_login` | `customer_user` | [portal.py:366](../../gdx_dispatch/routers/portal.py#L366) |
| `portal_password_login_failed` | `customer_user` | [portal.py:352](../../gdx_dispatch/routers/portal.py#L352) |
| `portal_booking_created` | `booking_request` | [portal.py:611](../../gdx_dispatch/routers/portal.py#L611) |
| `portal_message_sent` | `portal_message` | [portal.py:645](../../gdx_dispatch/routers/portal.py#L645) |
| `portal_estimate_accepted` | `estimate` | [portal.py:903](../../gdx_dispatch/routers/portal.py#L903) |
| `portal_estimate_declined` | `estimate` | [portal.py:996](../../gdx_dispatch/routers/portal.py#L996) |

**The bug:** these rows set `user_id = str(customer_user.id)` — a `CustomerUser`
UUID. `_resolve_user_names` looks IDs up in the **`User`** table
([audit.py:69-72](../../gdx_dispatch/routers/audit.py#L69-L72)), misses, and
the frontend's UUID guard renders **`Unknown user (a1b2c3d4)`**. So the moment a
customer does log in, the feed will misreport it as an unknown staff member.

Fix, folded into Phase 2's resolver:

- Resolve `user_id` against `CustomerUser` as a second pass when the `User`
  lookup misses, joining to `Customer.name` for the label.
- Return a new `actor_type` field (`staff` | `customer` | `system` | `api_key`)
  so the UI can distinguish them — a customer accepting an estimate and a
  dispatcher accepting one on their behalf are very different events and must
  not look identical.
- Render customer actors with a distinct icon/badge, e.g.
  *"Estimate E-1042 accepted — by ⟨customer⟩ (customer)"*.

Prod check: only `portal_invite_sent` ×1 and `portal_access_toggled` ×2 exist
today — no customer has ever logged in. There is no history to surface, so this
is forward-looking instrumentation. It also means 5a is **untestable against
real prod data** until a customer actually uses the portal; verify with a
seeded portal user on a throwaway container instead.

#### 5b — "Customer opened your estimate / invoice" (small, high value)

Two public, unauthenticated endpoints already exist and are exactly what the
customer hits when they click the link in an email. Neither writes an audit row:

- `GET /pay/{invoice_token}` — server-rendered Stripe payment form,
  [core/payments.py:458-485](../../gdx_dispatch/core/payments.py#L458-L485)
- `GET /proposals/{token}` — public proposal/estimate view,
  [modules/proposals/router.py:42-48](../../gdx_dispatch/modules/proposals/router.py#L42-L48)

Adding a `log_audit_event_sync` to each yields `invoice_viewed_by_customer` /
`estimate_viewed_by_customer` with `user_id="customer"`, `actor_type="customer"`,
and the customer resolved via the document's `customer_id`.

This is a **better signal than an email open** — it means the customer actually
clicked through, not that a mail client fetched an image — and it costs two
audit calls and zero new infrastructure.

Details to get right:

- **De-dupe.** A customer refreshing the pay page 8 times must not produce 8
  rows. Suppress repeats within a window (e.g. 30 min per token) by checking
  the most recent matching row before writing.
- **Bot filtering.** Link scanners in corporate mail gateways and Safe Links
  will hit these URLs. Skip known bot user-agents and log the UA in `details`
  so false "views" are diagnosable rather than mysterious.
- **Write cost.** `/pay/` is public and unauthenticated; an audit write per GET
  is a DoS amplifier. The de-dupe check bounds it, but rate-limit the endpoint
  independently.

#### 5c — True email open tracking (larger, and honestly unreliable)

Worth being straight about the cost/benefit before committing:

**There is no open tracking today, and no provider to borrow it from.** Mail
goes out over the tenant's own SMTP (or SES) —
[core/email.py:58](../../gdx_dispatch/core/email.py#L58),
[core/email_sender.py:81](../../gdx_dispatch/core/email_sender.py#L81) — so
there are no ESP webhooks (SendGrid/Postmark-style) to subscribe to. Open
tracking has to be built here.

`campaign_sends.opened_at` exists in the schema
([modules/campaigns/router.py:73](../../gdx_dispatch/modules/campaigns/router.py#L73))
and is aggregated in the campaign stats query — but **nothing in production
code ever writes it**. Only `tests/test_marketing.py` sets it. It is a dead
column, and the campaign "open rate" it feeds is therefore structurally 0%.
That's a live reporting bug worth fixing or removing regardless of this plan.

To do it properly:

1. **New `email_sends` table** — there is no outbound-email record at all today
   for transactional mail (estimates, invoices, reminders). Needs
   `id, tenant_id, customer_id, entity_type, entity_id, to_email, subject,
   sent_at, opened_at, open_count, last_open_ip, last_open_ua`. This is the
   real cost of 5c: transactional email is currently fire-and-forget, so
   "which emails did we send" doesn't exist as data either. (Worth building on
   its own merits — it also answers "did the customer ever get the invoice?")
2. **Tracking pixel endpoint** — `GET /t/o/{send_token}.gif`, public, returns a
   1×1 GIF, records the open, never 404s in a way that renders as a broken
   image.
3. **Inject the pixel** into outbound HTML mail in the send path.
4. Emit an `email_opened` audit row (de-duped like 5b) so it reaches the feed.

**Accuracy caveat — this is the part to weigh.** Apple Mail Privacy Protection
(on by default since iOS 15) pre-fetches every image the moment mail arrives,
producing an "open" whether or not a human looked. Gmail proxies and caches
images, so a second read of the same message often records nothing. For
consumer email — which is most of this customer base — open tracking produces a
number that is inflated on one side, undercounted on the other, and not
trustworthy at the level of "did this specific customer read my estimate."

Click-through (5b) does not have this problem. **Recommendation: ship 5b, and
treat 5c as optional** — worth it mainly if the `email_sends` record itself is
wanted for deliverability, in which case opens are a cheap add-on to a table
being built anyway.

**Privacy.** A tracking pixel in customer email is surveillance the recipient
did not consent to, and it sits oddly beside a codebase that already keeps a
GDPR access log. If 5c ships, it should be a tenant setting (default off), and
the same `data_accessed` reasoning in Phase 4 applies: customer-behavior
tracking may belong in a compliance view rather than a dashboard widget.

---

## Tests

- `test_activity.py` / `test_audit_compliance.py`: assert `user_name` and
  `entity_label` are present and correct for a customer/job/invoice row; assert
  an unknown entity_type degrades to `null` rather than 500.
- Resolver query-count test — one page of 50 mixed rows must not exceed one
  query per distinct entity_type.
- Route-sweep actor test (Phase 3, above).
- Vitest on `DashboardView`: a fixture row renders actor and entity name, and a
  `system`/UUID actor renders the guarded fallback.
- Existing `data-testid="recent-activity-list"` /
  `activity-item-${id}` hooks are already in place for a Playwright check.
- Phase 5a: seed a `CustomerUser`, log in through the portal, assert the row
  resolves to the customer's name with `actor_type="customer"` — **not**
  `Unknown user (…)`. This is the regression that exists today.
- Phase 5b: hit `/pay/{token}` and `/proposals/{token}` twice in quick
  succession, assert exactly one audit row (de-dupe), and assert a known bot
  user-agent produces none.

## Verification

Per the usual definition of done — not "tests pass":

1. `/verifyplaywright` against local code, real account, light **and** dark.
2. Deploy, then read the live dashboard as Doug's own account and confirm the
   top 10 rows name a person and a customer/estimate.
3. Re-run the prod `audit_logs` actor-distribution query a day after Phase 3
   ships: the `system` share of new rows should collapse from 85% to roughly
   the true background-job share.
4. Phase 5b: send a real estimate and a real invoice to a mailbox we control,
   click both links, confirm two named customer-view rows appear on the
   dashboard — end-to-end, on prod, with real mail.

## Open decisions for Doug

1. **Phase 4 noise policy** — hide line-level edits, collapse them, or leave
   them? Affects whether Phases 1–2 actually feel like a fix.
2. **`data_accessed` on the dashboard** — keep it as activity, or move it to
   the compliance/audit page only? It's 78 rows and rising, and it is a *read*,
   not a change.
3. **Sequencing** — Phase 1 alone is a ~30-minute visible win and can ship on
   its own. Phase 3 is the one that matters for the audit trail's integrity but
   touches 54 handlers across 17 routers. Ship 1 first, or batch 1+2+3?
4. **Is 5c (pixel open-tracking) worth it?** It needs a new `email_sends`
   table, a pixel endpoint, and send-path changes, and Apple/Gmail image
   handling makes the resulting numbers untrustworthy. 5b (click-through)
   gives a truer answer for a fraction of the work. Build 5c anyway?
5. **Does the `email_sends` record have independent value?** Right now there is
   no record of which transactional emails were sent to whom. "Did the customer
   ever get this invoice?" is unanswerable today. If that's worth having on its
   own, 5c becomes cheap and the decision in (4) flips.
6. **Dead campaign open-rate.** `campaign_sends.opened_at` has no writer, so
   campaign open rate always reports 0%. Fix as part of 5c, or remove the
   metric so it stops lying?
