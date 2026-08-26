# Estimate Option Groups — good/better/best as one opportunity

**Status:** PLAN (2026-08-18) — not built. Adversarially audited 2026-08-18;
the audit's two confirmed holes (duplicate-as-template welding strangers into a
group; add-on sale blocked by the one-accept guard) are fixed in §3/§4 below.
**Problem owner:** Doug
**Related:** tier lines (v1.58.0, same-door good/better/best inside one estimate),
duplicate endpoint (`POST /api/estimates/{id}/duplicate`), sales-funnel close rate.

## The problem

Good/better/best for door models is issued as **3 separate estimates** — usually
duplicates of the first (`EST-000042`, `EST-000042-1`, `EST-000042-2`). When the
customer accepts one, the sale happened; the other two were never independent
opportunities. But today:

- Staff must **decline** the siblings, and the mandatory loss-reason rule forces
  them to invent a fake reason — or they let the siblings rot to `expired`.

- The dashboard close rate (`GET /api/reports/sales-funnel`,
  `routers/reports.py:925–1026`) counts decisions as
  `accepted + declined + expired`. One sale presented as 3 options reads as
  **1-of-3 = 33%** when the true close rate is **100%**.
- `GET /api/estimates/pipeline-summary` sums all open option estimates —
  **3× pipeline dollars** for one opportunity.
- Estimate nurture (`routers/estimate_nurture.py`) follows up on `draft`,
  `sent`, **and `declined`** — it will nag a customer who already bought.

- The "outstanding estimates" aging tile counts un-closed siblings as
  outstanding work.

Nothing in the schema links the options: no `group_id`, no `parent_id` — only
the `-N` number convention (a string) and an `estimate_duplicated` audit row.

## Why not "just use tiers"?

Tiers (`proposal_mode` + `proposal_tiers`) are the right shape when the options
are variants of **the same door** (price/warranty ladder). Different door
models carry different descriptions, photos, and often different line
structures — a separate estimate document per model is the correct workflow,
and it's what the office actually does. Both mechanisms coexist:

- **Tiers** = good/better/best *within* one estimate (one document, one link).
- **Option groups (this plan)** = good/better/best *across* estimates
  (several documents, one opportunity).

## Design

### 1. Schema: `option_group_id` on `estimates` + a small group table

- New nullable column `option_group_id` (String(36) UUID, indexed) on
  `estimates`.

- New table `estimate_option_groups` (ORM `create_all`, house pattern — no
  migration for new tables): `id` (UUID PK — the value estimates carry in
  `option_group_id`), `company_id`, `public_token` (String(64) unique, minted
  at creation like `estimates.public_token` — used by the combined options
  page, §8), `combined_link_override` (nullable Boolean — NULL inherits the
  company setting), `created_at`. A row is created whenever a group id is
  minted (§3). `option_group_id` stays a plain indexed string, **no DB-level
  FK** (keeps the guarded ADD COLUMN portable); integrity lives in the
  service layer, which only ever stamps ids it just created or read.

- A standalone estimate has `option_group_id = NULL` and is treated everywhere
  as a group of one.

**Migration 069** (estimates is bootstrap-created, so follow the house pattern
for columns on existing tables — guarded `ADD COLUMN IF NOT EXISTS`, must run
on both SQLite and Postgres):

- `ADD COLUMN IF NOT EXISTS option_group_id VARCHAR(36)` + index.
- New enum value (next section): on Postgres,
  `ALTER TYPE estimate_status ADD VALUE IF NOT EXISTS 'not_selected'`
  (no prior migration touches this type — first of its kind; on SQLite the
  column is plain VARCHAR, no-op). Note: `ALTER TYPE ... ADD VALUE` cannot run
  inside a transaction block on older PG — use autocommit/`op.execute` outside
  the transactional DDL if needed.

### 2. New terminal status: `not_selected`

`draft, sent, accepted, declined, rejected, expired` + **`not_selected`**.

Semantics: *this option was closed because a sibling option in the same group
was accepted.* It is not a loss (`declined`/`expired`) and not a bounce
(`rejected`). It requires **no loss reason** — the reason is structural and
recorded as the accepted sibling.

Why a new status instead of reusing `declined`:

- `declined` drives loss reporting, nurture ("win-back"), and the mandatory
  loss-reason rule — all wrong for a won opportunity's leftovers.

- Downstream consumers can then treat it correctly without heuristics.

Status-enumeration touch list (all must learn the new value):

- `modules/proposals/models.py:39` (Enum)
- `routers/estimates.py:664` (list filter pattern)
- `core/mcp_tools/estimates_list.py:56`
- `routers/portal.py:53` (`CUSTOMER_VISIBLE_ESTIMATE_STATUSES` — include it;
  the portal shows a terminal "another option was chosen" state, below)

- `core/job_display_state.py` — do **not** add to `_ESTIMATE_LOST`;
  `not_selected` on a job's estimate must not paint the job "Lost" when the
  group actually sold. Map it to a neutral/terminal display.

- `frontend/src/utils/statusSeverity.js` (severity: `secondary`/neutral)
- `frontend/src/views/EstimatesView.vue` status tabs (fold into a tab — see UI)
- Reopen: `POST /{id}/reopen` gate (`routers/estimates.py:2543`) adds
  `not_selected` to `{expired, declined, rejected}` — escape hatch if the
  customer changes their mind or staff closed the wrong one.

- Expiry paths (`expire-stale`, `tasks/estimate_expiry.py`) — no change needed
  (they only touch `sent`/`draft`/`rejected`), but tests must pin that
  `not_selected` is never expired over.

- Nurture (`routers/estimate_nurture.py:134,233`) — `not_selected` is already
  excluded by not being in the include-list; **additionally** exclude any
  estimate whose group has an accepted member (fixes today's bug where `sent`
  siblings keep getting reminders after the sale, and `declined` win-back
  nagging a buyer).

### 3. Grouping happens at Duplicate — but only while the opportunity is open

`POST /api/estimates/{id}/duplicate` already exists and is how ~95% of options
are made (Doug 2026-07-30). But Duplicate is **also** used as a template cloner
(new customer, new sale from an old estimate's lines) — the audit confirmed
staff can re-point `customer_id` after duplicating (`routers/estimates.py:931`).
Grouping rules must not weld unrelated sales together:

- **Join/mint only from an open source.** If the source status is
  `draft`/`sent`/`rejected`: mint a group (row in `estimate_option_groups`,
  §1) if the source has none, stamp its id on both source and copy; else the
  copy joins the source's group.

- **Duplicating a terminal estimate** (`accepted`/`declined`/`expired`/
  `not_selected`) creates an **ungrouped** copy — that's a new opportunity
  (the add-on second-door sale, the template clone), not another option on a
  finished one.

- **Auto-detach on customer re-point.** If an update changes `customer_id` on
  an estimate that has an `option_group_id` (to a different customer than its
  siblings), clear `option_group_id` and audit it
  (`estimate_option_detached`, reason `customer_changed`). A group is only
  meaningful as one customer's choice set — this keeps that invariant true
  under the template-clone workflow.

- **Manual detach**: a small "Remove from options" action on the estimate
  detail (endpoint clears `option_group_id`, audited with the actor). Cheap,
  and it's the escape hatch the accept guard (§4) points at.

- The `estimate_duplicated` audit row gains `option_group_id` in details.

The office keeps doing exactly what it does; the rules above only decide when
the stamp is applied. A revision-style duplicate of an open estimate (the ~5%)
also lands correctly: original and revision are one opportunity, the accepted
revision closes the original as `not_selected`, and the group counts once.

### 4. Accept resolves the siblings (all five accept paths)

New service function, e.g. `modules/proposals/service.py::resolve_option_siblings(db, estimate, actor)`:

- If `estimate.option_group_id` is NULL → no-op.
- Else: every sibling in the group with status in `{draft, sent, rejected}`
  **and the same `customer_id` as the accepted estimate** (NULL-from-
  soft-delete counts as same) transitions to `not_selected` (stamp
  `updated_at`; leave `declined_*` NULL). The customer filter is defense in
  depth on top of §3's detach rules — resolution must never close another
  customer's live estimate. Siblings already `declined`/`expired`/`accepted`
  are left untouched (a real prior decline keeps its history).

- **Audit each sibling**: `log_audit_event(action="estimate_not_selected",
  details={accepted_estimate_id, accepted_estimate_number, option_group_id})`
  with the acting user (invariant #1: who/what/when reconstructable).

- Guard: accepting an estimate whose group **already has an accepted member**
  → 409 ("another option in this group was already accepted — remove this
  estimate from the group, or reopen the accepted one"). With §3's rules a
  legitimate second sale never lands in a won group (terminal duplicates
  start ungrouped), and manual detach is the escape hatch — the guard never
  requires destroying a real accept.

Call sites (every path that writes `status = "accepted"`):

1. Staff accept — `routers/estimates.py:2338`
2. Public token accept — `modules/proposals/router.py:481`
3. Staff tier accept — `modules/proposals/service.py:202` (`accept_tier`)
4. Customer portal accept — `routers/portal.py:1225`
5. Mobile/truck accept — `routers/mobile_quoting.py:604`

Decline of a single option stays exactly as-is (a customer can decline one
model and still buy another — that decline is real per-option history, and the
group only counts as lost when *everything* is lost; reporting handles that).

### 5. Group-aware reporting

**Close rate** (`routers/reports.py` sales-funnel) — aggregate by
`COALESCE(option_group_id, id)`:

- **Window anchor:** a won group is windowed on the **accepted member's**
  `sent_at` (degrades exactly to today's formula for standalones, and a
  later-sent sibling can never drag an old win into the current window); an
  un-won group is windowed on its most-recent `sent_at`. Drafts-only groups
  stay excluded, as today.

- A group is **won** if any member is `accepted`.
- A group is **decided** if it is won, OR every sent member is terminal-lost
  (`declined`/`expired`; `not_selected` never occurs without a win;
  `rejected` = bounce stays excluded, as today).

- `close_rate = won_groups / decided_groups`. Standalone estimates degrade to
  the current formula exactly.

- Dashboard sub-label copy changes from "N of M sent estimates" to
  "N of M opportunities".

- Implementation note: fetch the window's rows and aggregate in Python — the
  30-day set is small, single-tenant; don't contort SQL.

**Sold tiles** (`_sold_window`): already correct (counts `accepted` only) —
unchanged; pin with a test.

**Pipeline summary** (`routers/estimates.py:598`): count each group **once**,
valued at its **lowest-sell** member (DECIDED, Doug 2026-08-18: the floor —
keeps pipeline from being overvalued; the customer is guaranteed to pick at
most one option, and the conservative read is the useful one). Concretely:
among a group's members that pass the endpoint's existing filters
(`job_id IS NULL`, status in draft/sent/accepted, ≥1 line), pick the
lowest-sell member and include **only that member's lines** in every
aggregate — sell *and* cost come from the same representative, so
`blended_margin` stays internally consistent. Members excluded by the current
filters (e.g. job-attached) stay excluded; the "3× pipeline dollars" problem
only exists for unattached option sets, and this fixes exactly those.
Standalone estimates unchanged.

**Outstanding-estimates aging**: siblings get auto-closed at accept, so the
tile self-corrects; groups still open just show their members as today
(acceptable — they *are* each outstanding docs a customer is holding).
Optional later: collapse to one row per group.

**Dead endpoint**: `GET /api/estimates/analytics/conversion-rate`
(`routers/estimates.py:2777`) has no frontend caller and an
every-estimate-ever denominator — **delete it** in the reporting PR (filed in
the PR body, not silently).

**Historical repair**: because the metric aggregates by group, backfilling
`option_group_id` onto old duplicate families (next section) fixes past close
rates by itself; the backfill's tidy step (flipping won groups' leftover
siblings to `not_selected`) is list hygiene on top and does not move the
metric further.

### 6. Backfill (one-shot script, not a migration)

**DECIDED (Doug 2026-08-18): audit-proven links only; bulk-tidy to
`not_selected`.**

- Sole source of truth: `estimate_duplicated` audit rows
  (`details.source_estimate_id` / `new_estimate_number` → link source and
  copy; transitive chains — duplicate of a duplicate — union into one group).
  The `EST-NNNNNN(-N)` base-number convention is **NOT** used for linking
  (Doug: audit-proven only). Same-customer guard still applies: an
  audit-linked pair whose rows now carry different customers is reported, not
  grouped (that was a template clone).

- Grouping step: create the `estimate_option_groups` row, stamp members.
  Skip pairs where either member is already in a group.

- **Tidy step (Doug: yes):** in every backfilled group with exactly one
  `accepted` member, flip the remaining same-customer `draft`/`sent`/
  `rejected` members to `not_selected` — the same rule live resolution
  applies (§4), run once over history. Each flip writes an
  `estimate_not_selected` audit row with `details.backfill: true`. Groups
  with zero accepts, or (defensively) more than one, are left untouched and
  listed in the report.

- Output: a report of every group formed (numbers, statuses, customers),
  every status flipped, and the exceptions list (customer-mismatch pairs,
  multi-accept groups). Report pasted before calling it done.

- Run on prod after deploy: deploy PR A–C first (so the status renders
  properly), then run, then check the dashboard close rate against the
  report's expected numbers.

### 7. UI

**Estimates list** (`EstimatesView.vue`):
- Option-group badge on rows in a group ("Option 2 of 3"), tooltip listing
  siblings; clicking filters to the group.

- `Not Selected` gets a neutral tag. Tabs: fold `not_selected` into the
  existing set — recommend a single "Closed" tab is *not* introduced; instead
  add `Not Selected` to the tab row only if counts warrant, else it lives
  under All. (Small decision, defer to build.)

**Estimate detail** (`EstimateView.vue`):
- "Options" strip when `option_group_id` is set: each sibling as a chip —
  number, total, status — linking to it, plus the "Remove from options"
  detach action (§3) and the per-group combined-link control (§8). This is
  the sibling-navigation staff currently do by memory.

- Duplicate confirm dialog copy: "Creates option EST-000042-2 in this
  estimate's option group."

- Accept flow: after accept, response includes how many siblings were closed;
  toast: "Accepted. 2 other options marked Not Selected."

- A `not_selected` estimate shows a banner: "Customer chose EST-000042-1"
  (link), with Reopen available.

**Mobile** (`MobileEstimatesView.vue:350–354`): the `open` filter excludes
`['declined','accepted','converted','rejected']` — add `not_selected` (and
note the list is missing `expired` today, a pre-existing gap: fix it in the
same line or file it, don't drop it silently). Show the neutral tag on detail.

**Public approval page** (`ProposalPublicView.vue` +
`modules/proposals/router.py` serializer): a customer opening the link of a
`not_selected` option sees a friendly terminal state: "You chose a different
option for this project" (mirrors the existing accepted/declined terminal
messages). Accept/decline buttons hidden; API accept on a `not_selected`
token returns the same 409-family error as other terminal states.

### 8. Combined public options link (DECIDED: build it, behind a setting)

**Doug 2026-08-18:** one link showing all options, as an option settable from
the estimates surface or company-wide settings.

**The setting, two levels:**

- **Company-wide default**: a Settings toggle, e.g. "Send good/better/best
  options as one link" (tenant settings). Default **OFF** at ship (Doug flips
  it company-wide when the walk proves it) — matches the
  automation-emails-default-OFF precedent.
- **Per-group control on the estimates surface**: the Options strip (§7)
  shows the group's combined link with copy button and an On/Off override
  (`estimate_option_groups.combined_link_override`; NULL = inherit company
  setting). So the office can run one-link for a given customer even before
  flipping it company-wide, or exempt one group.

**Send behavior** (when effective setting is ON for the group):

- The send/compose flow for any member offers "Send all options as one link"
  — one email carrying the **group** link instead of the member's link.
- Sending the group link marks every included member as `sent` (stamp
  `sent_at`, `sent_via`) — they are now all in front of the customer, and the
  close-rate window keys off `sent_at`. Included = the group's same-customer
  members in `draft`/`sent`; terminal members are excluded from the page and
  from stamping.
- Individual per-estimate links keep working regardless — the group page is
  additive, never a lockout. Turning the setting OFF later never dead-links a
  customer: already-sent group URLs keep resolving; OFF only stops offering
  the link in the send flow.
- The group email rides the existing single send pipeline (branded shell,
  `outbound_emails` log) from the v1.68 email overhaul — no second sender.

**The page** (`/options/{group_token}`, public, noShell — sibling of
`ProposalPublicView`):

- One card per open option: label, description, total (and tier picker inside
  a card if that member is `proposal_mode` — reuse the existing tier-pick
  contract). Pick → confirm → accepts that estimate through the existing
  public-accept semantics (deposit flow and auto-convert included), which
  auto-closes the siblings (§4) — the page then shows the chosen-option
  terminal state.
- "None of these" → declines **all** open members with one optional reason
  (public declines keep reason optional, as today).
- After any decision the page is terminal: "You chose EST-000042-1" /
  "You declined these options".

**Hard-rule checks (customer-facing):** unauthenticated reachability +
token enumeration review of the new group token (64 chars, minted like
estimate tokens; page serializer is an explicit projection like
`_serialize_public_estimate`); phone test — cards readable and tappable on a
small screen, no login wall.

### 9. Tests (enumerated, not exhaustive)

- Duplicate of an **open** source mints/joins group, both rows stamped, audit
  details carry group id; duplicate of a **terminal** source stays ungrouped
  (the add-on-sale path: duplicate accepted → accept the copy → succeeds).

- Template-clone path: duplicate, re-point `customer_id` → auto-detach fires,
  audited; accepting either estimate never touches the other.

- Accept via **each of the 5 paths** closes same-customer `draft/sent/rejected`
  siblings to `not_selected`, leaves `declined/expired` untouched, skips a
  different-customer sibling, writes one audit row per sibling with the actor.

- Second accept in a group → 409; after manual detach, the accept succeeds and
  the original accept is untouched.

- Manual detach endpoint: clears group id, audited, idempotent.
- Reopen from `not_selected` → draft; group id survives reopen.
- Close rate: 3-option group with 1 accept + 2 auto-closed = 1/1 = 100%;
  3-option group all declined = 0/1; mixed standalone + group windows;
  `rejected` still excluded; drafts-only group excluded.

- Pipeline: group counted once at its lowest open member's value.
- Nurture: no reminder for any member of a won group.
- Public page: `not_selected` token renders terminal state, accept 409s.
- Expiry tasks never touch `not_selected`.
- Migration runs on SQLite and PG (enum ALTER PG-only, guarded).
- Combined link: group page lists only open same-customer members; accept via
  the group page closes siblings + shows terminal state; decline-all declines
  every open member; sending the group link stamps `sent_at`/`sent_via` on
  every included member; override beats company setting in both directions;
  OFF (company + no override) → no group-link offer in send flow and the
  group URL still resolves for already-sent links (never dead-link a
  customer); backfill tidy flips exactly the report's list, audited with
  `backfill: true`.

### 10. PR packaging (stacked, bottom-up)

1. **PR A — core:** column + migration 069, enum value, duplicate stamping
   (open-source-only rule), auto-detach on customer re-point, manual detach
   endpoint, `resolve_option_siblings` (customer-filtered) + 5 call sites +
   409 guard, reopen support, audit events, backend tests.
2. **PR B — reporting:** group-aware close rate + pipeline summary, dashboard
   copy, delete dead conversion-rate endpoint, tests.
3. **PR C — UI:** list badge, detail options strip + Remove-from-options
   action, not-selected banners/tags, mobile + public page terminal states,
   frontend tests + browser walk (light/dark, desktop/mobile).
4. **PR D — backfill:** script (audit-proven grouping + tidy step) + report;
   run on prod after A–C are deployed.
5. **PR E — combined options link:** settings toggle (company-wide), per-group
   override + link control on the Options strip, group send flow, public
   `/options/{token}` page, reachability/enumeration review, frontend tests +
   phone-size browser walk.

Each lands with the full test matrix enumerated and a throwaway-container
browser walk per the working agreement. E stacks on C; D can land any time
after A but runs on prod only once A–C are deployed.

## Decisions (Doug, 2026-08-18)

1. **Combined public options page — BUILD IT**, as an option settable from the
   estimates surface or company-wide settings (§8, PR E). Ship default OFF;
   Doug flips company-wide after the walk.
2. **Pipeline group value = LOWEST-value open member** (Doug: the floor —
   keeps the numbers from being overvalued).
3. **Backfill: audit-proven links only** — the number-convention fallback is
   dropped (§6).
4. **Historical siblings of won groups: bulk-tidy to `not_selected`** in the
   backfill, audited, report pasted (§6).

## Explicit non-goals

- No multi-tenancy, ever (single-tenant invariant).
- No change to tier behavior — tiers and option groups coexist.
- No customer-tax anywhere near this (MN construction contract rule untouched;
  this plan touches no money math — totals, tax, deposits all unchanged).

- No retro status rewrites beyond the decided backfill tidy (§6) — nothing
  else in history is touched.
