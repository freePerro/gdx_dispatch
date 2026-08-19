# Jobsite Address: Mobile Visibility + "Same as customer?" Ask

**Date:** 2026-08-18 (re-reviewed same day against CLAUDE.md working agreement)
**Status:** PLAN — not built
**Ask (Doug):** "In mobile it shows the customer address for the job but it doesn't make
the jobsite address visible. We should also have something that asks if it is the same
as the customer address in jobs and estimates."

---

## 1. What exists today (research findings)

There are **two parallel jobsite systems**, and neither reaches mobile:

### A. Jobs — structured, works on desktop, invisible on mobile

- `Job.location_id` → `customer_locations` (label, address, access_notes, is_primary,
  city/state/zip/lat/lng) — `models/tenant_models.py:347`, `:2993`.
- Backend validates the location belongs to the customer at job create/update
  (`routers/jobs.py:203`), and `GET /api/jobs/{id}` already returns
  `location_label` / `location_address` (`routers/jobs.py:3199-3215`).
- The office list `GET /api/jobs` already LEFT JOINs `customer_locations` and emits
  `location_label`/`location_address` per row (`routers/jobs.py:775-778`).
- Desktop `JobDetailView.vue:1519-1556` has the full precedence logic:
  picked location → customer primary location → customer.address, **with the /audit
  2026-05-21 rule**: a picked location with a NULL address must surface as
  "address missing", never silently substitute the customer HQ.
- Both create forms have a location picker, but it **only renders when the customer
  already has 2+ locations** (`JobsView.vue:283`, `MobileJobNewDialog.vue:582-583`).
  A single-site customer whose jobsite differs from their billing address has **no
  path to say so at create time** without first adding a location on the customer screen.

### B. Estimates — free text, never carried forward

- `Estimate.jobsite_address` (Text) — `modules/proposals/models.py:20`. Rendered on the
  estimate PDF (`routers/pdf.py:189`), portal (`portal.py:1083`), public proposal
  (`proposals/router.py:286`), and searched (`search.py:152`).
- Desktop `EstimateView.vue:299-302` is a passive textarea plus a
  "Use as jobsite" copy button (`:251-254`). Blank is ambiguous — could mean
  "same as customer" or "nobody filled it in".
- The mobile quote dialogs (`MobileQuoteBuilderDialog.vue`,
  `MobileCustomerQuoteDialog.vue`) don't capture a jobsite at all.

### C. The gaps

1. **Mobile never shows the jobsite.** Every mobile endpoint serializes only
   `customer.address` and builds `navigation_link` from it:
   - `_job_card` for `/api/mobile/today` — `routers/mobile.py:944-1007`
   - `/api/mobile/job/{id}` detail — `mobile.py:2141-2171`
   - `/api/mobile/jobs` (Jobs tab) — `mobile.py:1570-1611` (`c.address` only)
   - `/api/mobile/my-jobs` — `mobile.py:1624+` (raw SQL, same pattern)
   - `/api/mobile/schedule` — `mobile.py:795-898` (no known Vue consumer — legacy)
   - day summary — `routers/mobile_day_summary.py:75-78`, `:206-209`
   `job.location_id` is ignored everywhere. A tech on a multi-location customer's job
   navigates to the customer HQ — exactly the field-error the desktop /audit note
   was written to prevent, alive on the surface techs actually use.

2. **Estimate→job conversion drops the jobsite.** `_create_job_from_estimate`
   (`routers/estimates.py:1861-1918`) copies lines/title/description but not
   `jobsite_address`, and never sets `location_id`. A sold install at a different
   address becomes a job that shows the customer's billing address — on desktop AND mobile.

3. **No "same as customer?" ask** anywhere. Estimate: passive field. Job: picker
   hidden for the 1-location common case.

Side finding (sweep): `MobileDispatchView.vue:550` renders `job.address` from
`GET /api/jobs?date=...`, but that SELECT doesn't include `c.address` at all — that
span is likely permanently empty. Fix falls out of PR 1's serializer work.

---

## 2. Design decisions (defaults stated; flip if you disagree)

- **D1 — One resolution rule, shared.** "Effective jobsite" = bound location's address
  (`location_id`) → customer primary location → `customer.address`. Same precedence as
  desktop `JobDetailView`. Implemented ONCE as a backend helper and reused by every
  mobile serializer, so mobile and desktop can never disagree.
- **D2 — Never silently substitute HQ.** Bound location with NULL address ⇒
  `site_address: null` + `site_address_missing: true`; the UI shows the label and
  "No address on this site — ask dispatch". (Carries the desktop /audit rule to mobile.)
- **D3 — The ask reuses the structured system.** Answering "No, different address" on
  job create writes a real `customer_locations` row (existing
  `POST /api/customers/{id}/locations`) and binds `job.location_id`. No new schema,
  no second free-text address on jobs (the `CustomerContact` docstring's
  "a second one here would just be a thing to disagree with it" rule).
- **D4 — Estimates keep `jobsite_address` text, but "Same as customer" freezes a copy**
  of the customer address into it at save. Estimates snapshot everything else
  (pricing, tiers); the address the customer approved should not drift if the
  customer record is edited later. Blank stops being ambiguous: the field is always
  populated once a customer is picked.
- **D5 — Conversion bridges text → structure.** On estimate→job, if
  `jobsite_address` differs (normalized) from the customer's address, find-or-create a
  matching `customer_locations` row and set `location_id` on the new job.
- **D6 — Debt filed, not built:** migrating `Estimate.jobsite_address` to a
  `location_id` FK (full convergence of the two systems). Follow-up, tracked in
  CLEANUP_BACKLOG.md.

---

## 3. The rungs (4 PRs, bottom-up; PR 4 depends on PR 1)

Each PR goes through the CLAUDE.md pipeline: `/audit` on its slice of this plan
**before code**, then build → full matrix → throwaway browser walk → sibling sweep →
Verification Manifest → (after deploy) prod walk.

### PR 1 — Mobile shows the jobsite (no schema change)

**The user:** a tech on a phone in a driveway, deciding where to drive next.

**Backend** (`routers/mobile.py`, `routers/mobile_day_summary.py`):
- New helper (suggested: `core/job_site.py`) `resolve_job_site(db, job) ->
  {site_label, site_address, site_address_missing, access_notes}` implementing D1/D2.
  Batched variant for list endpoints (one query for the page's `location_id`s +
  one for primary locations of the page's customers — no N+1 on the Today hot path).
- Apply to: `_job_card` (today/area cards), `/api/mobile/job/{id}`,
  `/api/mobile/my-jobs/{id}`, `/api/mobile/jobs`, `/api/mobile/my-jobs`, and the
  day summary's `next_first_stop`. Emit `site_label`, `site_address`,
  `site_address_missing` alongside the existing `customer.address` (additive —
  nothing removed, offline-queued clients keep working).
  **Pre-code audit trims (2026-08-18):** `/api/mobile/schedule` is skipped
  entirely (consumer-less, §6 deprecation candidate) and the day summary's
  `jobs_completed` recap keeps the customer address (history, not navigation).
- `navigation_link` is built from the **effective site address**, not
  `customer.address`, at every builder call site.
- **Pre-code audit adds (§3):** the `/today` drive-time enrichment
  (`compute_drive_times`) feeds site addresses, and the appointment map pin is
  SUPPRESSED when the job is bound to a location whose address differs from the
  customer's — the pin was geocoded from whatever address dispatch had, and a
  wrong pin is worse than a missing one. Dispatch-side geocoding of the bound
  site is a follow-up (§6).
- Access notes: mobile job detail gets the bound/primary location's `access_notes`
  (gate codes are the thing the tech in the driveway needs). Same helper, same row.
- Fix the side finding: `/api/jobs` list serializer feeds MobileDispatchView a real
  address (it already selects `location_address`; add the customer fallback).

**Frontend:**
- `MobileJobDetailView.vue` (address row `:69-79`): row becomes **Jobsite** — shows
  `site_label` chip when a location is bound, `site_address` as the tappable
  navigation link, and the D2 missing-warning state. If the customer's address
  differs from the site address, show it as a secondary muted "Customer address" row.
- `MobileTodayView.vue` (`:1236-1241`, `:1473-1475`) and `MobileJobsView.vue`
  (`:105-109`, `:183`): card address prefers `site_address`, keeps the
  "No address — ask dispatch" empty state.
- `MobileSummaryView.vue` / `MobileDispatchView.vue`: same substitution.
- The resolution helper lives in `core/job_site.py` from this PR; PR 3's and PR 4's
  normalized find-or-create joins it there so all three share one module.
- PR 1's missing-address empty state may say "ask dispatch" — PR 4 replaces it with
  an actionable "Add it" as soon as it lands.

**Tests:** pytest — helper precedence (bound / bound-with-null-address / primary /
customer fallback), each endpoint's payload, navigation_link source; vitest — detail
view renders label + missing state; card fallback.

### PR 2 — "Same as customer address?" at job create

**The users:** office staff at a desk creating/editing a job; a tech creating a job
from the phone.

`JobsView.vue` create/edit dialog + `MobileJobNewDialog.vue`:
- Once a customer is picked, always render the ask:
  **"Jobsite same as customer address?" [Yes] [No]** — default **Yes** (ships
  `location_id: null`, which already means "customer's primary/address").
- **No →**
  - customer has locations: today's picker (now always reachable, not gated on 2+),
    plus a "New address…" option;
  - "New address…" (or zero locations): inline address field (+ optional label).
    On submit: `POST /api/customers/{id}/locations` → bind the returned id as
    `location_id`. Failure of the location POST blocks job submit with a visible
    error (never silently create the job at the wrong address).
  - Reusing that POST is deliberate (invariant #1): it already writes the
    `create_customer_location` audit event (`routers/customers.py:741`), so the new
    row carries who/what/when for free. No new mutation endpoint is created.
- Edit dialog gets the same ask seeded from the job's current `location_id`.
- New-customer path in `MobileJobNewDialog` already captures one address — that stays
  the customer address; the ask still applies after (rare, but a new customer can have
  a different jobsite on day one).

**Tests:** vitest — default Yes ships null; No+new address chains the POSTs and binds
the id; POST failure blocks submit. pytest — none needed (endpoints exist).

### PR 3 — Estimates: the ask + conversion carries the jobsite

**The users:** the office estimator writing the quote; the customer opening the
proposal on their phone; downstream, the tech who inherits the converted job.

**EstimateView.vue:**
- Replace the passive textarea + "Use as jobsite" button with the same ask:
  **"Jobsite same as customer address?"** default **Yes**. Yes → `jobsite_address`
  is set to the customer's address at save (D4 freeze). No → the textarea, required
  non-empty. Seed the toggle on load: existing estimate whose `jobsite_address`
  matches the customer address (normalized) → Yes; differs → No; blank (legacy) → Yes.
- Mobile quote dialogs (`MobileQuoteBuilderDialog`, `MobileCustomerQuoteDialog`):
  same ask, minimal form (a "Different address" disclosure with one field). Verify
  their create endpoints (`routers/mobile_quoting.py`) accept/persist
  `jobsite_address`; add if missing.

**Conversion** (`routers/estimates.py:1861` `_create_job_from_estimate`):
- If `estimate.jobsite_address` is set and normalized-differs from
  `customer.address`: find a non-deleted `customer_locations` row for that customer
  with a normalized-equal address; else create one (label `"Jobsite (EST-xxxxxx)"`,
  address = the estimate text). Set `new_job.location_id`.
- Same-or-blank → leave `location_id` NULL (customer-address fallback is correct).
- Normalization: casefold + collapse whitespace/commas. Find-or-create makes
  re-accepts and repeat estimates at the same site converge on ONE location row
  instead of minting duplicates.
- **Auditability (invariant #1):** the auto-created location row is a mutation with
  no router of its own, so `_create_job_from_estimate` writes its own
  `log_audit_event` (`create_customer_location`, details:
  `{source: "estimate_conversion", estimate_id}`), attributed to the acting user —
  or `_PUBLIC_ACTOR` on the public-accept path, matching how the conversion itself
  is already attributed. An unaudited auto-created row would be invisible to the
  "who put this address on this customer?" question.
- **No silent drops:** if the find-or-create/bind fails, the accept must still
  succeed (existing guard pattern, `estimates.py:2067-2074`) — but the failure may
  not be silent. Log + audit event (`estimate_jobsite_bind_failed`) **and** append
  the raw jobsite text to the new job's notes, so the address the customer approved
  is preserved on the job even when the structured bind didn't happen.

**Tests:** pytest — conversion binds/creates/skips correctly, idempotent on the same
address twice; PDF/portal/public-proposal still render `jobsite_address` (they read
the same field — D4 means they now render it more often, which is the point).

---

### PR 4 — "Fix this address" from the driveway (depends on PR 1)

**The user:** a tech standing at the real jobsite, looking at a wrong or missing
address on the phone. Today their only path is "call dispatch". The mobile detail
screen already lets techs fix missing emails and contacts ("you're the one who can
fix that", 219 of 382 customers had no email) — this is the same affordance for
the address.

**UI** (`MobileJobDetailView.vue`):
- Pencil on the Jobsite row; the missing/empty states ("No address — ask dispatch",
  D2's "No address on this site") become actionable: "Add it".
- One sheet, pre-filled with the current effective address, with the PR 2 question
  mirrored — where does this fix apply?
  - **No location bound** (address came from the customer record):
    - "Fix the customer's address" (default) — corrects the record everywhere.
    - "This job is at a different place" — find-or-create a location + bind this job.
  - **Location bound:**
    - "Fix this site's address" (default) — edits the bound location; microcopy
      states plainly "updates this site for all its jobs". No confirm dialog —
      `useDestructiveConfirm` auto-accepts (issue #215), so the honest label IS the
      guard, per the sharp-edges list.
    - "This job is actually somewhere else" — find-or-create + rebind, leaving the
      original site's record untouched.
- Affordance hidden in the read-only/company-grant state — write access rides the
  job, same as every other action on the screen.
- Offline-first via `patchQueued`/`postQueued`, exactly like the add-email path
  ("a tech gets the email at the door, which is exactly where the signal isn't").

**Backend:**
- Extend `PATCH /api/mobile/jobs/{id}/customer` (`mobile.py:3765`) to accept
  `address`. Same permission (`customers.contact_write` — same trust level as
  editing the customer's phone), same `_assert_job_access`, same audit shape
  (field NAMES only, never the value — `Customer.address` is encrypted PII and the
  audit row is not the place to republish it). **ORM attribute set only** — the
  `_customer_for_job` docstring's raw-SQL warning applies doubly here since
  `address` is `EncryptedString`.
- New job-scoped endpoint `PATCH /api/mobile/jobs/{id}/site` with
  `{address, apply_to: "site" | "new_site"}`:
  - `"site"` — job must have `location_id`; updates that location row's address.
  - `"new_site"` — normalized find-or-create (shared `core/job_site.py` helper from
    PR 3) + rebind `job.location_id`.
  - `_assert_job_access` (write grant), audit event per branch
    (`mobile_job_site_updated` / `mobile_job_site_rebound`, details: job_id,
    location_id, source field names) — invariant #1.
- Refresh of `/api/mobile/job/{id}` after the write shows the corrected Jobsite row
  (PR 1's serialization) — the tech sees the fix land.

**Tests:** pytest — all four sheet branches, read-only tech 403, audit rows written,
find-or-create convergence (fixing the same wrong address twice doesn't mint two
locations); vitest — sheet defaults per bound/unbound state, hidden when read-only.

## 4. Traps (from this research)

1. **`Customer.address` is `EncryptedString`** (`tenant_models.py:158`). ORM reads
   decrypt; **raw-SQL reads don't** — every mobile raw-SQL path wraps with
   `decrypt_if_ciphertext`. The resolve helper must compare/serve *decrypted* values;
   any new raw SQL selecting `c.address` needs the same wrap + `# noqa: RAW_ENC`.
   `customer_locations.address` is plaintext Text — no wrap, don't "fix" it.
2. **N+1 on the Today path.** `_job_card` is per-row; location resolution must be
   batched per request, not per card (the duration-computation precedent at
   `jobs.py:820-828` is the house rule).
3. **D2 everywhere.** Label-only location rows are legal; never fall through to the
   customer address once a location is bound.
4. **Offline queue compat.** Mobile POSTs run through `postQueued`; PR 2's
   location-then-job chain must fail loud, not enqueue a job pointing at a location
   that was never created.
5. **`location_id` ownership guard.** Job create 400s on a location that doesn't
   belong to the customer (`jobs.py:203`) — PR 2's UI must clear the binding when the
   picked customer changes (JobsView already does at `:754-756`; keep that behavior).
6. **Conversion runs from public accept too** (`modules/proposals/router.py:533-535`,
   `_PUBLIC_ACTOR`) — the find-or-create must not assume a logged-in office user, and
   a failure to bind the location must not sink the accept — but per the working
   agreement's no-silent-writes rule, the failure path logs, audits, and preserves
   the address text on the job (see PR 3).
7. **No new unauthenticated write surface** (hard rule: unauth reachability check).
   The public accept must never take a client-supplied address — conversion reads
   ONLY the stored `estimate.jobsite_address` the office wrote. The token-holder
   gains no new ability to write customer records; verify this in PR 3's tests
   (accept payload with an injected address field is ignored).
8. **jsdom proves nothing about layout** — the new mobile detail row needs the real
   browser/android walk, both themes.

## 5. Verification (per the CLAUDE.md build pipeline — evidence, not assurance)

Per PR, in order:

1. `/audit` the PR's slice of this plan before writing code.
2. Full test matrix — every FAIL and SKIP enumerated **by name** (isolate the ~15
   known cross-file flakes before blaming the branch), lint checked against the
   **ruff ratchet baseline**, not plain pass/fail. pytest via the docker-app image
   (never a real DB); vitest for the touched forms + mobile views.
3. Throwaway container + **headed** browser walk, as the real tech role (techs
   role-redirect to /mobile), light AND dark, desktop AND mobile viewport:
   - PR 1: job bound to a non-primary location → Today card, detail Jobsite row,
     and the navigation link all show the site, not the HQ.
   - PR 2: create a job answering "No, different address" → location row exists on
     the customer, job bound; POST failure path blocks submit visibly.
   - PR 3: estimate with a different jobsite → accept via the **public** proposal
     page on a phone-sized viewport (customer-facing phone test) → converted job
     shows the jobsite on mobile. Injected address in the accept payload is ignored.
   - PR 4: as the tech, fix a wrong address from the job detail → Jobsite row and
     navigation link update; "different place" branch creates+binds a location
     without touching the customer record; affordance absent on a read-only job.
4. Sibling sweep reported with scope + result (the §1C endpoint enumeration is the
   sweep's checklist — every `customer.address` serialization site accounted for).
5. Verification Manifest written per PR before commit (with Sibling Sweep and
   "Where it stands" sections).
6. After deploy: **prod walk is the finish line** — Micheal's real jobs on
   `/mobile` show the right address, real navigation link opens the right pin.

## 6. Debt / follow-ups filed

- `Estimate.jobsite_address` (text) → `location_id` FK convergence (D6).
- `/api/mobile/schedule` appears consumer-less — candidate for deprecation sweep
  (verified during the PR 1 audit: only the generated `types/api.d.ts` mentions it).
- Dispatch-side appointment geocoding uses whatever address the dispatch client
  had — geocode from the effective jobsite so bound jobs always get a fresh pin
  (PR 1 re-pins from `customer_locations.lat/lng` when stored, keeps the
  appointment pin when its own address matches the site, else suppresses).
- `/api/jobs`' raw LEFT JOIN of `customer_locations` duplicates the resolver's
  bound-row fetch (post-code audit §2b) — consolidate consumers onto
  `site_*` fields, then drop `location_label`/`location_address` from the SELECT.
- Day-summary `jobs_completed` + labor-hours window predicates share the
  SQLite ISO-'T'-vs-space datetime-compare bug found (and fixed for
  `next_first_stop`) in PR 1 — sweep the file's remaining raw datetime ranges.
- MobileDispatchView `job.address` dead-field cleanup lands with PR 1.

(Tech-side address correction was originally filed here as a follow-up; Doug pulled
it into scope 2026-08-18 — it is now **PR 4**.)

## 7. Open question (product shape)

- **PR 3 mobile quote dialogs:** the tech quoting in a driveway is usually standing AT
  the jobsite. Default ask remains "Same as customer? Yes" — fine, or should mobile
  default to "capture nothing extra" (skip the ask entirely on mobile, office adds it
  later)? Plan assumes: show the ask on mobile too, default Yes, one tap to differ.
