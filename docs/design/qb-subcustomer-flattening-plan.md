# QuickBooks Sub-Customers Flattened Into Customers

**Date:** 2026-08-19
**Status:** PARTIALLY BUILT — **PR 1 MERGED #372**; **PR 2 MERGED #373**;
**PR 3 built** (GDX-wins + per-row audit). **Not built:** PR 4 (duplicate
detector by email/phone), PR 5 (the data cleanup). Adversarially audited three
times on 2026-08-19 (PR 1 as plan, PR 1 as diff, PR 2 as diff); findings folded
into §4 and §5.
**Ask (Doug):** "Somewhere along the way bob at riverbend lumber became jeff."
Follow-up: "can we fix all of that. for riverbend lumber they are all job names."

> **Read this before believing PR 2 fixed the account.** On production the six
> real sub-customers all still carry `entity_type='customer'` maps, so a pull
> takes the LEGACY branch for every one of them: `sites=0, legacy_subs=6`.
> PR 2 stops NEW flattening and keeps the legacy rows correct; **PR 5 is what
> actually consolidates the account.** The QB refresh token also expired
> 2026-07-07, so no pull can run at all until someone reconnects.

---

## 0. What already exists (do not rebuild)

| Thing | Where | State |
|---|---|---|
| `customer_contacts` — many people per customer | `models/tenant_models.py:234` | **Built**, 0 rows in prod |
| Mobile add/list/delete contact | `routers/mobile.py:4080-4185` | Built, tech-only, job-scoped |
| `GET /customers/{id}/contacts` | `routers/customers.py:1291` | Built, **no UI caller** |
| Make-primary + recipient picker | `customers.py:1318`, `EstimateView.vue:2908` | Built, shipped v1.68.0 |
| `customer_locations` — saved sites | `models/tenant_models.py:3027` | **Built**, 0 rows in prod |
| Job → location binding + jobsite ask | PRs #356–#359, merged | Built |
| Customer merge endpoint | `routers/customers.py:1189` | Built |
| Duplicate detector | `routers/customers.py:1008` | Built, **name-only** |

Two shipped tables with zero production rows. Nothing here needs building from
scratch; the gaps are the paths that reach them.

**Rival/adjacent plan:** `jobsite-address-visibility-plan.md` owns
`customer_locations` and the jobsite ask. Its header says "PLAN — not built" but
PRs #356–#359 are merged (`66ed758`, `f70efdb`, `a8d9021`, `8acc087`) — the
status line is stale and is corrected in PR A of this plan. This plan does not
supersede it; it fills the table that plan created.

---

## 1. What actually happened

QuickBooks models a project as a **sub-customer** (`ParentRef`) — what QB's UI
calls a "Job". Riverbend Lumber is QB customer `35`; six sub-customers hang
under it. `pull_customers` reads only `DisplayName` and never looks at
`ParentRef`, so on 2026-04-13 03:10 UTC it created all six as **top-level GDX
customers** (77 customers in that one minute overall), each inheriting the
parent's `PrimaryEmailAddr`:

| GDX customer | QB id | email | created |
|---|---|---|---|
| Riverbend Lumber | 35 | (parent) | 2025-06-09 |
| Site C | 121 | same | 2026-04-13 03:10:16.68 |
| Site B | 122 | same | …16.68 |
| Site D | 131 | same | …16.70 |
| Site E | 138 | same | …16.71 |
| Site F | 139 | same | …16.69 |
| **Site A** | 140 | same | …16.67 |

Doug confirms: **all six are job names**, not people and not separate accounts.

Nothing was renamed. "Sam became Site A" is a *split*: the account's history sits
on the parent (15 invoices, 6 estimates, 2 jobs) while new work is being written
against a sub-row. EST-000096 (draft, $0) and EST-000098 ($1,330.21, created
2026-08-20 00:04 UTC) are both on the "Site A" row.

The push side already has the opposite rule locked in
(`sync.py:1408`, /audit 2026-05-21): *"one GDX customer ↔ one QB customer; site
differentiation lives in the memo, not in sub-customers."* The pull side never
got that memo. **This plan makes the pull side obey the rule the push side
already follows.**

## 2. Three defects, one symptom

**D1 — the pull flattens sub-customers.** `sync.py:611`. No `ParentRef` read.
**D2 — the duplicate detector cannot see it.** `customers.py:1012` groups by
normalized *name* only. "Site A" ≠ "Riverbend Lumber", so the screen that
exists to catch this never will. Prod: **29 of 306** active customers share an
email with another row.
**D3 — the pull clobbers local edits with no per-row trail.** `sync.py:584`
assigns `name`/`email`/`phone` unconditionally; a QB row with no
`PrimaryEmailAddr` **blanks** a good GDX email. The only audit is one run-level
row (`qb_pull_customers`, 2026-07-21, actor `system`, 260 updated) — counts, no
before/after, so prior values are unrecoverable. The 2026-04-13 run left no
audit row at all.

Plus **D4 — the office cannot record a second person at all.** No contacts UI on
`CustomerDetailView.vue` (the word "contact" does not appear in it). The only
way to create a contact is a tech tapping through a mobile job screen — which is
why the table has zero rows, and why QB sub-customers were the only place a name
like "Site A" could land.

## 3. Decisions (locked with Doug 2026-08-19)

- **QB sub-customer → saved site on the parent** (`customer_locations`), not a
  customer, not a GDX job. Matches the push-side lock; GDX jobs are work orders
  and six fake work orders would be a lie.
- **GDX wins on locally-edited rows.** New `customers.local_edit_at`; the sync
  fills blanks but never overwrites a field a human changed in GDX. QB is being
  phased out — it is no longer the authority on customer identity.
- **Cleanup scope: the tool + Riverbend only.** The other 23 shared-email rows
  stay for Doug to review in the UI once the detector can see them.
- **The daily inbound QB sync is set to `manual`** (done on prod 2026-08-19
  00:49 UTC; `qb_sync_schedule.frequency` daily → manual, `next_run_at` → NULL).
  It pulled ten inbound entity types nightly including `customer_payments`, and
  was only inert because the OAuth token is dead — a reconnect would have
  silently resumed it. Outbound push is untouched.
- **Contacts UI ships first.** D4 is the root cause: sub-customers became the
  dumping ground because the office had nowhere to put a person.

## 4. The work — five stacked PRs, merged bottom-up (order set post-audit)

### PR 1 (was D) — the office can record a second person
- `POST` / `PATCH` / `DELETE /api/customers/{id}/contacts` (office, audited).
  Only mobile job-scoped writes exist today, and **no edit path exists
  anywhere** — a typo'd contact email currently means delete and re-add.
- Contacts panel on `CustomerDetailView.vue`: list, add, edit, remove, set
  primary. Wires up the orphan `GET` endpoint.

### PR 2 (was A) — the pull stops flattening sub-customers
- Read `ParentRef` on each QB Customer. When present: resolve the parent's GDX
  customer, upsert a `customer_locations` row labeled with the leaf name, and
  map it as `qb_entity_maps.entity_type = "customer_location"`.
- **Two-pass**: QB returns children before parents (ids 121–140 vs 35 — the
  parent has the *lower* id, but ordering is not contractual). Pass 1 handles
  every row without a `ParentRef`; pass 2 handles children, so the parent map
  always exists. A child whose parent is missing after pass 1 is recorded as a
  row error, never silently promoted to a top-level customer.
- Also corrects the stale status line on `jobsite-address-visibility-plan.md`.

### PR 3 (was B) — GDX wins, and the overwrite is audited
- Migration: `customers.local_edit_at TIMESTAMPTZ NULL`. Set by
  `PATCH /customers/{id}` (`customers.py:608`) and the mobile customer patch
  (`mobile.py:3914`).
- Pull: when `local_edit_at IS NOT NULL`, GDX owns name/email/phone — the sync
  fills blanks only. Otherwise QB fills but **never blanks** —
  `if qb_email: customer.email = qb_email`.
  **Audit catch:** the first draft compared `local_edit_at > synced_at`. That is
  broken — `_upsert_map` re-stamps `synced_at` on *every* pull including no-ops
  (`sync.py:499`), so the comparison goes permanently False and "GDX wins"
  silently stops winning for every row, forever. No timestamp race: presence of
  `local_edit_at` alone decides ownership.
- Per-row `qb_customer_overwritten` audit naming **which** fields changed, never
  the values (repo precedent: `mobile.py:3925`).

### PR 4 (was C) — the duplicate detector sees shared email and phone
- Group by normalized email and by phone last-4 in addition to name; each group
  reports what matched so the reviewer can tell a true dupe from a sub-customer
  split. Surfaces all 29 rows.

### PR 5 (was E) — the Riverbend cleanup (destructive; runs last)
- Extend the merge path to "absorb as saved site": move each sub-row to a
  `customer_locations` row on the parent, repoint its work, soft-delete the row,
  repoint `qb_entity_maps` to the new location.
- Riverbend only: 6 rows → 6 saved sites; EST-000096 and EST-000098 move to the
  parent with `jobsite_address` set to the job name (estimates have **no**
  `location_id` — only free-text `jobsite_address`, `proposals/models.py:20`).

## 5. Traps (rewritten after the adversarial audit)

1. **`is_primary` is the entire blast radius — and the first draft never named
   it.** `core/job_site.py:162`: `customer.address` is read **only** for
   customers with no primary location, and `job_site.py:211-223` lets a primary
   location replace the jobsite for every unbound job. Creating a primary
   location on Riverbend would make its 2 existing jobs stop showing
   `No Address Provided, riverbend, MN, 56551` and start showing
   `address_missing`. **Every location this plan creates is `is_primary=False`.**
   Precedent already in the repo: `job_site.py:252` forces `is_primary=False`
   for exactly this reason.
2. **The six have no address.** All six sub-rows are `address IS NULL` in prod;
   QB carries the address on the parent. These become label-only sites, which
   downstream surfaces render as "⚠ Site A — no address on file"
   (`JobDetailView.vue:304`) and the dispatch board relabels `Riverbend · Site A`
   (`DispatchView.vue:1262`). Accepted and expected — but PR 2 must also read
   `BillAddr`/`ShipAddr` off the sub-customer when QB has one, and PR 5 must not
   collapse six NULL-address rows into one via a normalized-address match.
3. **`_detect_qbo_merge_deletes` (`sync.py:256`) is always-on — no feature
   flag.** It probes any `entity_type='customer'` map missing from `seen_qb_ids`
   with a live `GET Customer/{id}` and soft-deletes the local Customer on
   `Active=false`. **Live risk today, unrelated to this plan:** a QBO user
   marking a finished job inactive — the normal thing to do — soft-deletes the
   GDX row carrying EST-000098. Its flagged sibling `_apply_qbo_deletes` is OFF
   (`delete_sync_enabled` NULL, `QB_DELETE_SYNC_ENABLED` unset → default OFF),
   which is what the first draft checked and then stopped. Checking whether the
   flag was off is not the same as checking whether the flag was the only door.
4. **PR 5 resurrects itself unless PR 2 is DEPLOYED first.**
   `_adopt_existing_customer:190` filters `Customer.deleted_at.is_(None)`, so
   soft-deleted rows are invisible to adoption — the next customer pull creates
   six brand-new top-level customers. **Merge is not enough; PR 2 must be
   released to prod and the pull quiet before PR 5 runs.** The daily schedule is
   now `manual`, and customer pulls only fire from two manual endpoints
   (`router.py:449`, `:613`) — so the window is controllable, not automatic.
5. **Sub-customer ids must stay in `seen_qb_ids`** after PR 2, or trap 3's
   always-on probe fires six extra **metered** QBO reads every pull, forever.
6. `model_map` in `_apply_qbo_deletes:391` has no `customer_location` entry — the
   new map type gets no delete detection at all. Deliberate for now, recorded so
   it is not mistaken for coverage.
7. **Type mismatch, already solved upstream**: `customer_locations.customer_id`
   is `varchar`, `customers.id` is `uuid`. `job_site.py:117-119` documents and
   solves this; `_discover_customer_fk_tables` (`customers.py:1163`) already
   sweeps the column by name. Reuse, do not re-derive.
8. **`humanize_name` — checked, not assumed.** All six job names pass through
   unchanged. `'Riverbend Lumber:Site A'` also survives *with* the colon, so
   the leaf-split must be explicit; QB's qualified field is
   **`FullyQualifiedName`**, not `DisplayName`.
9. `customer_locations.company_id` is NOT NULL.
10. Every migration runs on **both SQLite and Postgres**; escape literal `%`
    as `%%`.
11. PR 2 prevents *new* flattening only. Existing rows need PR 5.
12. `useDestructiveConfirm` auto-accepts silently (issue #215) — the PR 1 delete
    control cannot rely on it to actually confirm.
13. **The six are heterogeneous**: one reads as a job type, one as a business,
    one as a person's first name. Doug's call is that all six are
    job names, so all six become sites; recorded because one bucket for six
    different shapes is the kind of thing that looks obvious later.

## 6. Rollback

- PR B migration: `local_edit_at` is additive and nullable; downgrade drops it.
- PR E is the only data mutation. Sub-rows are **soft-deleted**, never dropped;
  `qb_entity_maps` rows are rewritten, and the pre-state is captured in an audit
  row before the move. Reversal = clear `deleted_at`, repoint the estimates back,
  drop the created locations.
- No money math changes. No invoice, payment, or total is touched.

## 7. Out of scope (filed, not bundled)

- The other 23 shared-email rows (Doug reviews in the UI after PR C).
- Contact `*_hash` sidecars so an inbound email/call from a contact resolves to
  the customer — the model docstring already calls this a separate change.
- QB sub-customer **push** (GDX saved site → QB sub-customer). The push-side
  lock says memo, not sub-customers; that decision stands.
- **Mobile's contact writes audit AFTER their commit** (`mobile.py`
  `add_mobile_job_customer_contact`, `delete_mobile_job_customer_contact`).
  Same defect class as the one fixed on the office side in PR 1: a failed audit
  cannot roll the change back. Mobile's `_audit_mobile` swallows-and-logs by a
  documented deliberate choice ("the write it describes is already committed"),
  so converting it to `audit_or_rollback` + `audit_ready_db` is a change to that
  subsystem's audit philosophy, not a bug fix inside this feature. Filed, not
  bundled — found by the second adversarial audit 2026-08-19.
- **`GET /customers/{id}/contacts` has no read gate** — any authenticated user
  can enumerate any customer's contacts. Pre-existing on the whole router
  (`POST ""`, `PATCH /{id}`, `DELETE /{id}` are all ungated too); PR 1 gates
  only the writes it adds. The router-wide read authorization is its own piece
  of work.
- **The shared real-app test fixture.** PR 1 adds
  `tests/test_customer_contacts_http.py`, which builds its own `FastAPI()` like
  133 other files — `conftest.py` has zero references to `create_app()`. It
  keeps the REAL `require_permission` gate so its 403 test is behavioral, but it
  still cannot see the GL flush guard, the webhook dispatch hook, the rate
  limiter, or app.py's global exception handlers. Doug's call 2026-08-19: the
  `create_app()`-based client fixture ships as its own PR after this one, with
  the two contact test files as its first migration.
