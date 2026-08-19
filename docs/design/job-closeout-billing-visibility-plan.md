# Closeout → billing visibility: plan

**Status:** PARTIALLY BUILT — shipped v1.32.0+ (status line added 2026-08-18;
this doc had none, and the corpus audit `design-doc-corpus-audit-2026-08-18.md`
marked it STALE). **Not built:** the §8 unpriced-item policy at line 469 is
half-shipped — the priced-only draft exists, but "mark the invoice needs
pricing and block send until resolved" was built on the mobile lane only
(`routers/mobile_invoicing.py:838-846`). The office lane surfaces nothing, so
`require_deliverable` verifies a draft with attested parts missing and no
indication — the rubber-stamp failure §10 predicted at line 913. Fix planned in
`closeout-parts-autopricing-plan.md` PR 3, which builds this doc's decision
rather than making a new one.

**Filed** 2026-07-29. **Trigger:** a tech finished JOB-2026-006 and neither he nor
the office could see the hours or anything else about the work in a form usable
for billing. Follow-on reports in the same conversation: "sometimes we do
invoices for a job and when the job is closed it doesn't match things up
correctly — it will send it to billing even though we have been paid", and
"trouble creating a job and putting info in and saving it".

Everything below was traced against production (read-only queries) on
2026-07-29. Each item is marked **VERIFIED** (reproduced in prod data or a
guaranteed-by-code path) or **HYPOTHESIS** (mechanism identified, not yet
observed in current data).

---

## 0. The headline: the data was never lost

JOB-2026-006 (a shed door service call) has a complete,
correct closeout row in prod:

| field | value |
| --- | --- |
| `hours_worked` | **3.00** (tech-entered; job was added after the fact) |
| `notes` | a real three-sentence description of the work performed |
| `no_parts_used` | `true` |
| `signed_by` | customer name present, `signed_at` NULL (no signature image) |
| `closed_at` | 2026-07-29 19:39:28Z |

Plus a matching `time_entries` row: 180 minutes, closed, `hourly_rate` 50.00,
note "Closeout-attached".

Nothing is missing from the database. Every problem below is a **read-side**
problem: capture works, surfacing does not.

---

## 1. `job_closeouts` is a write-only table — VERIFIED

`POST /api/jobs/{job_id}/closeout` (`routers/jobs.py:1575`) is the **only** route
in the codebase that touches `job_closeouts`. There is no GET, at any path. The
single reader anywhere is `routers/tech_efficiency.py:79`, which uses the row as
an existence check plus an hours number for a ratio.

So `hours_worked`, the tech's work-performed notes, the parts attestation, and
`signed_by` go into Postgres and never come back out to any screen.

What each side sees today:

- **The tech (mobile).** `views/MobileJobDetailView.vue:395-405` prints
  "Arrived … · closed out …" and the caption *"Hours for this job come from what
  you entered at close-out"* — and then never prints the hours. It names the
  source and withholds the number. The deliberate refusal to imply a duration
  from `arrived_at`→`completed_at` is correct and should stay; the missing piece
  is the attested number itself.
- **The office (desktop).** No closeout data at all. The only hours display
  anywhere is a Time Entries table buried in the **Costing** tab of
  `views/JobDetailView.vue:710` — not on the billing path, and broken (§3).

### Fix

1. Add `GET /api/jobs/{job_id}/closeout` returning the snapshot (hours, notes,
   `parts_used`, `no_parts_used`, `signed_by`/`signed_at`, `closed_by`
   resolved to a display name, `closed_at`). Permission: same read gate as job
   detail, so both office roles and the assigned tech can read it.
   **Must redact `signature_data` on a company-wide grant** — audit finding A4:
   `routers/mobile.py:2053-2058` already nulls the raw signature blob for a tech
   browsing under `techs_see_all_jobs` ("never ship the customer's raw signature
   blob to a tech with no claim on the job", /audit 2026-07-22). A new closeout
   endpoint that returns the snapshot verbatim would walk straight around that
   control. `signed_by`/`signed_at` metadata still goes out — "was it signed" is
   legitimate context.
2. **Office:** a "Work performed / Closeout" card on `JobDetailView`'s
   **Overview** tab (not Costing) — hours, notes, parts-or-"tech attested no
   parts", who closed it and when, signature status. This is the card the
   office reads to bill.
3. **Tech:** on `MobileJobDetailView`, in the existing Time card, add the
   attested hours and the notes he submitted, so he can confirm what he sent.
4. Include the closeout hours in the invoice-build path so labor is not
   retyped from memory (see §7 — needs a decision on rate).

---

## 2. The tech cannot open the billing page at all — VERIFIED

The completing tech's role is `technician`. `core/permissions.py:186-199` grants
`technician` no `invoices.*` permission, and `GET /api/jobs/ready-for-billing`
is gated on `invoices.read_all` (`routers/jobs.py:1940`). Prod logs show this
firing for real, twice in the last 96h:

```
Client error logged: kind=api_error page=/billing detail=[api_error] Missing permission: ['invoices.read_all']
```

So "for him to do billing" is currently impossible through the office UI — he
gets a permission error on `/billing`. His only billing path is the mobile
flow (`POST /api/mobile/jobs/{job_id}/invoice`).

### Decided (Doug, 2026-07-29): techs bill from the field

Techs bill from mobile; the office page stays office-only. Two consequences:

- Hide/remove the `/billing` entry point for `technician` so it stops throwing
  a permission error at them. No permission grant needed.
- The field billing path becomes the closeout itself — see §8. `technician`
  already holds `pricing.labor_matrix.read`, which is exactly the gate on
  `GET /api/labor-pricing/items`, so the tech can read the price book with no
  permission changes.

> **⚠ CORRECTED by the audit (§10, finding A1).** An earlier version of this
> section claimed "a `technician` can already create an invoice, no permission
> work needed." That is **false in prod for most jobs.** The *role* is fine, but
> `mobile_invoicing._job_belongs_to_tech` is a broken ownership gate: its first
> check compares `jobs.assigned_to` to a **user** id, while that column holds a
> **technician** id in 22 of 22 rows — so it can never match. Ownership then
> rests entirely on the `job_assignments` fallback, and only **19 of 190**
> completed jobs have such a row. Field billing would 404 on the rest. See §10
> for the fix (use the already-correct shared gate).

---

## 3. The one hours table that exists renders wrong — VERIFIED

`views/JobDetailView.vue:710-718`, fed by
`GET /api/labor/jobs/{job_id}/time-entries`:

- **Tech column is always blank.** The column binds `field="technician_name"`,
  but `_entry_to_dict` (`routers/labor.py:114-129`) never returns that key —
  or any name key. On this job it would be blank regardless: the row's
  `technician_id` and `tech_name` are both NULL.
- **Clock In/Out render as raw ISO strings.** The columns use
  `:body="formatDateTime"`. In PrimeVue `body` is a *slot*, not a prop, so the
  formatter is ignored.
- **Techs can't see it anyway** — the endpoint is `_require_dispatch`
  (`routers/labor.py:64-70`).
- **The rate is a fallback, not the tech's rate.** The stored `hourly_rate` is
  50.00 because his `technicians.hourly_rate` is NULL and `labor.py` falls back
  to `DEFAULT_HOURLY_RATE` = $50. `job_costing.py` defaults to **$95**. Per the
  standing rule the stored value is never re-resolved, so this job is costed at
  $50/h by one reader and $95/h by the other, permanently.
- **`clock_out` is stamped in the future** — 22:38Z on a job closed at 19:39Z,
  because the entry closes at `clock_in + attested_minutes`. Harmless to
  costing, confusing on screen, and it will read as "still working" to a human.

### Fix

Return a resolved tech display name from `_entry_to_dict`; convert the columns
to `#body` slots; set `technician_id`/`tech_name` on the closeout-written row;
reconcile the two default rates to one constant (and surface "no rate on file"
rather than silently picking one); stop writing a future `clock_out` (clamp to
`now` and keep `duration_minutes` as the attested truth).

---

## 4. "Creating a job and saving it" — the save looks like it fails. VERIFIED

`views/JobsView.vue:1053-1063`, in the create branch, after the job POST
succeeds:

```js
const appointmentPayload = {
  job_id: jobId,
  date: formatDateForApi(jobForm.value.appt_date),
  time: jobForm.value.appt_time?.trim() || null,
  notes: jobForm.value.appt_notes || "",
};
await api.post("/api/appointments", appointmentPayload);
```

`AppointmentIn` (`routers/appointments.py:149-160`) requires **`title`**,
**`start_at`** and **`end_at`**. None are sent, and `date`/`time` are not
fields at all. **This request 422s every single time** the "schedule
appointment" box is ticked. Confirmed in prod logs:

```
page=/jobs detail=[api_error] [{"type":"missing","loc":["body","title"],"msg":"Field required",
"input":{"job_id":"…","date":"2026-07-30","time":"09:30","notes":""}}, {"type":"missing", …
```

The damage is in the error handling. Unlike the catalog-parts loop below it
(deliberately best-effort, wrapped), this `await` is unguarded, so the throw
unwinds to the outer `catch` and **skips everything after it**:

- `showFormDialog.value = false` never runs → the dialog stays open, still full
  of his data
- `await fetchJobs()` never runs → the new job never appears in the list
- the "Job Created" toast never fires; he gets a red error instead

**But the job was already created.** From the user's seat: filled the form,
hit save, got an error, dialog still open, job not in the list → "it didn't
save" → tries again → **a second job**.

This is almost certainly the source of the duplicate jobs in §5: every
duplicated job number is one old job plus one created on 2026-07-29.

### Fix

Build a real appointment payload (`title` from the job title, `start_at` from
date+time, `end_at` from `scheduled_duration_hours` or a default), wrap it
best-effort like the parts loop, and move `showFormDialog=false` + `fetchJobs()`
into a `finally`-style path so a post-create side quest can never make a
successful save look like a failure. Add a regression test that asserts the
job-create flow closes the dialog and refreshes the list even when a follow-on
call fails.

---

## 5. Job numbers collide, and most jobs have none — VERIFIED

Prod: **186 of 215** live jobs have `job_number` NULL. Five numbers are issued
twice — `JOB-2026-001`, `-005`, `-006`, `-008`, `-009` — all within the same
`company_id`, each pair being one job from May and one from 2026-07-29. There
are two live jobs called JOB-2026-006 right now, which is why the office
"doesn't match things up".

Mechanism: the counter went backwards. `tenant_settings.job_number_next_seq`
for the live tenant is at 10, having already issued 1–9 in May, and issued
1–9 again in July. `modules/numbering/router.py:105-117` lets a settings save
`PUT` any `seq >= 1` straight into the row with no check that the resulting
numbers are unused, and `Job.job_number` is declared `unique=False`
(`models/tenant_models.py:275`) so nothing at the DB level refused the
collision. (What triggered the reset is not recoverable from current state —
the guard is the fix either way.)

Second row: `tenant_settings` has a stray `00000000-0000-0000-0000-000000000001`
row at `seq=1`, `job_number_year_seen` NULL — never minted, but see §6.

### Fix

**Order matters — the audit caught a bug in my own sequencing (§10, finding A3):
the index must come LAST.** A partial unique index cannot be created while the
five duplicates still exist, so listing it first fails the migration on the way
in.

1. Renumber the five collisions (office-visible — needs your say-so on which of
   each pair keeps its number).
2. Backfill the 186 NULLs.
3. Make `next_job_number` skip-and-advance past any taken number instead of
   trusting the counter, and reject a settings `PUT` that would re-issue an
   existing number.
4. **Then** add the partial unique index on
   `job_number WHERE deleted_at IS NULL`.

---

## 6. Two `tenant_settings` rows disagree about the completion gates — VERIFIED

```
00000000-0000-0000-0000-000000000001 | require_signature=t | parts=t | hours=t
a1b2c3d4-e5f6-7890-abcd-ef1234567890 | require_signature=f | parts=t | hours=t
```

`_load_workflow_flags` (`routers/jobs.py:1185-1220`) reads
`WHERE tenant_id = :tid` and takes `.first()`. Whether closeout demands a
signature therefore depends on which `tenant_id` the request resolves to. A
real 422 landed on prod today:

```
POST /api/jobs/19285e50-…/closeout → 422 Unprocessable Entity
```

on a job that has no `job_number` and no `assigned_to`, and
which did eventually complete — i.e. the tech hit a gate, then got through.
Single-tenant-forever means one of these rows is stale. Reconcile to one row,
and have `_load_workflow_flags` log loudly if it ever sees more than one.

---

## 7. "Sends it to billing even though we have been paid"

**I could not reproduce a false positive in current data — stated plainly so
nobody builds on a guess.** Replaying the exact `ready_for_billing` predicate
against prod returns 13 jobs, and **every one has zero invoices attached**.
JOB-2026-006 is genuinely unbilled. (Its customer has two paid invoices, but
both are 2026-03-29 QuickBooks imports hanging off a synthetic import job, not
this work.)

Four mechanisms that would produce exactly the reported symptom, ranked by how
likely they are to be what you saw:

1. **Duplicate jobs — HYPOTHESIS, most likely.** With two live JOB-2026-006s
   (§4/§5), the office invoices one and the work sits on the other; the
   uninvoiced twin stays in Ready-for-Billing forever. Fixing §4 and §5 fixes
   this, and it explains why it's intermittent ("sometimes").
2. **Money that never enters GDX — VERIFIED as a data shape.** 23 live invoices
   have `job_id` NULL (all QB imports), and imports attach to a synthetic
   per-customer job. If the office bills in QuickBooks rather than GDX, the GDX
   job never acquires a billing-real invoice and never leaves the queue, no
   matter how paid it is. Worth deciding: should a paid QB invoice matched to a
   customer be linkable to a job so the queue clears?
3. **A latent SQL NULL bug in the canonical predicate — VERIFIED in code,
   dormant in data.** `job_billed_exists()`
   (`core/billing_predicates.py:52-60`) filters
   `Invoice.billing_type != 'deposit'` and `Invoice.status != 'void'`. In SQL a
   NULL on either column makes the comparison NULL, the `EXISTS` fails, and the
   job reads as **unbilled** — even if it is paid in full. Its Python twin
   `invoice_bills_job` coalesces (`(billing_type or "")`) and returns the
   opposite. Today prod has zero NULLs in those columns so nothing is
   misfiring, but the two halves are pinned against fixtures that always set
   the field, so the test suite would not catch the divergence. Add
   `IS NULL OR …` to the SQL side and a fixture with NULLs.
4. **The deposit carve-out — by design, not currently firing.** A fully-paid
   *deposit* invoice deliberately does not mark a job billed, so such a job
   lands in Ready-for-Billing on close. Prod has zero deposit invoices today,
   so this isn't your case yet — but it will read as "we've been paid and it
   still wants billing" the first time you take a deposit.

**Ask:** when you next see it, grab the job number. That tells us which of
these it is in one step.

---

## 8. Invoice from the closeout (the field-billing design)

**Doug 2026-07-29:** "let's have it so techs can bill from the field — in the
closeout, if everything is added correctly, we could create an invoice from it."

### What exists today

`POST /api/mobile/jobs/{job_id}/invoice` builds invoice lines from **exactly one
source: an accepted estimate**. With no estimate it creates a **$0 draft with
zero lines** (`routers/mobile_invoicing.py:415-570`). It never reads the closeout
hours and never reads the closeout's parts.

`GET /api/mobile/jobs/{job_id}/financial` *does* compute `labor_hours` and
`parts_cost` and show them to the tech — but they are display-only; nothing
carries them onto an invoice. That is the whole gap.

### Pricing basis — DECIDED: two lanes, keyed on `job_type`

Doug 2026-07-29: installs price flat from the matrix; **service calls get their
own hourly rate**. This removes the blocking data gap that a matrix-only design
had (the matrix contains installs and nothing else — see below).

| `job_type` | Basis |
| --- | --- |
| `Installation` | Flat `flat_price` from the matching `labor_price_items` row |
| `Service Call` / `Service` | First-hour minimum, then hourly (below) |
| anything else / NULL / `QB Import` | **No basis guessed** — invoice created with labor unpriced and flagged for the office |

Two data hazards to code around, both verified in prod:

- **Two spellings for one work kind:** `Service Call` (34 jobs) and `Service`
  (12). Normalize; don't match one literal.
- **159 legacy `QB Import` jobs.** These must never be auto-priced — they fall
  in the "office prices it" lane.

Never infer the lane from anything but `job_type`. `lifecycle_stage` is a
progress axis, not a work-kind axis, and its `service_call` value is the
repair-dispatch lane — conflating the two would price every sold install as a
repair.

### Service-call rate — DECIDED

- **First hour: minimum, billed in full** however short the visit.
- **Beyond the first hour: hourly.**
- **Rate: $100/hr.**
- **Billed time rounds UP to the next half hour.** Order of operations is
  fixed and must be documented in code: round up to the next 0.5, *then* apply
  the 1.0-hour floor.

Worked examples:

```text
attested 0.25 → round 0.50 → floor 1.00 → $100.00
attested 2.10 → round 2.50 → floor 2.50 → $250.00
attested 3.00 → round 3.00 → floor 3.00 → $300.00   ← JOB-2026-006
attested 3.60 → round 4.00 → floor 4.00 → $400.00
```

At $100 for both the first hour and each hour after, the structure collapses to
`max(1.0, roundup_half(hours)) × 100`. **Implement it as two separate settings
anyway** (`service_call_first_hour_price`, `service_call_hourly_rate`) so a
future "first hour $125, then $100" needs a settings change, not a code change.

**The attested hours are never overwritten.** Rounding and the floor produce a
*billed* quantity that lives on the invoice line; `job_closeouts.hours_worked`
and the `time_entries` row keep the exact attested figure for payroll and
costing. Two numbers, two purposes — this is the standing rule holding: pricing
policy may round a customer's bill, but nothing may rewrite what a tech attested.

$100/hr is also consistent with a rule already encoded in the system:
`pricing_settings.target_labor_blended_rate_per_hour` defaults to 100 with the
comment *"Per Doug rule: GDX targets $100/hr blended"*, and prod is set to
exactly 100.00.

### Where the rate lives

**`pricing_settings`** (tenant DB, `models/pricing_engine.py:107`) — add the two
columns next to `target_labor_blended_rate_per_hour` and
`loaded_labor_cost_per_hour`. That table already exists in prod and is the
established home for pricing config. Surface them in the Labor Matrix admin
screen, already gated on `pricing.labor_matrix.write` (admin/dispatcher write,
`technician` read-only — exactly right).

**Do not put it in `routers/pricing.py`.** Its `labor_rates.default` of $75 is
held in a module-level dict (`_PRICING_SETTINGS_BY_TENANT`, `pricing.py:90-93`)
— in-memory, per-worker, lost on restart. A money rate cannot live there. Worth
treating that $75 as a latent trap regardless of this work.

### Bonus finding: the correct cost rate was configured all along

Prod `pricing_settings` has **`loaded_labor_cost_per_hour = $65.00`** — the real
wage-plus-burden number. But the closeout wrote **$50** onto the tech's time entry
(`labor.py`'s hard-coded default, reached because no technician has an
`hourly_rate`), and `job_costing.py` would have said **$95**. So the one
correctly-configured cost rate is the one nothing reads.

Fold into §3: `_labor_rate_for` should fall back to
`pricing_settings.loaded_labor_cost_per_hour`, not a literal. On JOB-2026-006
that's a real difference — 3 hrs of cost is $195 at $65, and the row currently
says $150. With the new sell side at $300, the margin only reads correctly once
the cost rate is right.

### The matrix still needs service rows eventually — but it no longer blocks

All 8 `labor_price_items` rows are `service_type = 'install'` (8x7 through
20x14, $475–$1800). There are no repair/service items. Under the two-lane model
that's fine: service calls now price hourly and need no matrix entry, so
JOB-2026-006 (`job_type = Service Call`) bills
correctly on day one at 3.00 × $100 = $300.

Adding flat-price rows for common repairs later is an optimization (predictable
pricing on known jobs), not a prerequisite. When you do, the existing rows imply
about $100/man-hour, which lines up with the blended target.

### Flow

1. **Closeout gains a service picker.** Reads `GET /api/labor-pricing/items`
   (tech already has `pricing.labor_matrix.read`), filtered to `active` and
   effective-dated, size-aware. Tech picks one or more services; each becomes a
   flat-price labor line. Hours and notes are captured exactly as they are now.
2. **Closeout stays its own transaction, and invoicing is a separate step.**
   The closeout must never fail because pricing is awkward — and §4 is the
   cautionary tale of an unguarded side call destroying the primary action. The
   closeout response returns an "invoice-ready" summary; the app then calls the
   invoice endpoint.
3. **Extend `POST /api/mobile/jobs/{job_id}/invoice`** with a no-estimate path
   that assembles lines from:
   - **Labor:** the picked matrix items at `flat_price`.
   - **Parts:** `job_parts_needed` rows for the job with `source='closeout'` and
     `billed_invoice_id IS NULL`, priced from `unit_price` (the catalog sell
     price the closeout already stamps).
   - **Deposits:** reuse `apply_deposits_to_final` — already wired in this
     endpoint.
4. **Stamp `billed_invoice_id` in the same transaction**, reusing the office
   path's proven rule (`routers/invoices.py:986-1010`): stamp-first with
   `RETURNING`, and any row the stamp cannot claim → 409 rather than a silent
   skip that double-bills. This is the double-bill guard; do not reinvent it.
5. **Double-invoice guard:** mirror the office `create_invoice_from_job`
   behaviour — 409 unless `force=true` when `job_billed_exists()` already holds.
6. **Tax: DO NOT change tax behaviour in this work.** Keep the mobile path's
   current effective 0 and leave the question open — see §10 finding A2.

> **⚠ CORRECTED by the audit (§10, finding A2).** An earlier version of this
> step said to resolve tax via the shared resolver and that it "should come back
> 0 for customer sales tax." **Both halves were wrong.** Prod
> `tax_config.default_rate` is **0.0738** and `resolve_rate()` returns 0 only for
> customers explicitly flagged exempt — so following that advice would have
> started adding **7.38% sales tax to every field-billed invoice**, where the
> mobile path currently adds none. This needs a separate accountant-level
> decision, not a code change buried in a billing feature.

7. **Status:** create as `draft`. Sending stays the explicit existing step
   (`POST /api/mobile/invoices/{id}/send`) — but see §10 finding A5: on the
   hourly lane, sending must not be available to the tech until the office has
   seen the hours.

### Anything unpriced — DECIDED: create the invoice, flag it for the office

The tech is never blocked on-site. One sub-decision I'm making with a reason,
flag it if you disagree:

**Unpriced items should not become $0 lines on a customer-visible invoice.**
Instead: create the invoice from everything that *is* priced, leave the unpriced
rows unstamped so they stay on the office parts checklist, mark the invoice
"needs pricing", and **block send** until it's resolved. Nothing is silently
dropped, nothing wrong reaches a customer, and the tech still leaves with an
invoice started. A $0 line on a PDF the customer reads is worse than a flagged
draft.

This also keeps the existing `block_zero_price_on_invoice` policy intact rather
than carving an exemption into it — that guard is the reason a $0 tier can't
reach a customer from the truck today.

### Consequence for §7

Every invoice created this way carries a real `job_id`, so the job leaves
Ready-for-Billing the moment it's billed. That closes mechanism §7.2 for all
work billed from the field going forward — though it does nothing for money that
only ever exists in QuickBooks.

---

## 9. `job_type` has no canonical vocabulary — VERIFIED, fix it

**Doug asked whether the two spellings are deliberate. They are not.** This is
drift, and it is already breaking things independently of billing.

Prod holds exactly four values:

| `job_type` | count |
| --- | --- |
| `QB Import` | 159 |
| `Service Call` | 34 |
| `Service` | 12 |
| `Installation` | 10 |

### Root cause

`Job.job_type` is free-text `String(100)` with no enum and no constraint
(`models/tenant_models.py:292`), defaulting to `"Service"`. Independent code
paths then invented their own vocabulary against it — including **two frontend
dropdowns that disagree**:

- `views/JobsView.vue:619` → `["Service Call", "Installation", "Repair", "Maintenance"]`
- `views/CustomerDetailView.vue:761` → `["Service", "Installation", "New Construction", "Repair", "Maintenance", "Inspection", "Other"]`

So a job created from the **Jobs** page is `Service Call`, and the same job
created from **Customer detail** is `Service`. Backend writers disagree too:
`routers/service_calls.py:65` writes `"Service Call"`, `routers/jobs.py:72,850`
defaults to `"Service"`, `routers/estimates.py:1613` writes `"Installation"`.

Two more spellings are latent — they exist in code but have not reached prod
yet: `routers/instant_estimate.py:67,76` writes lowercase `"service"` /
`"installation"`, and `docker/demo/seed_demo.py:252` writes `"Install"`.

### What it already breaks (no billing involved)

- **`routers/service_calls.py:117`** filters `Job.job_type == "Service Call"`, so
  the **12 `Service` jobs are invisible** to the service-call queue.
- **`views/JobsView.vue:658`** — `isServiceCall` is false for those 12, so the
  form asks for "Job title" instead of "Problem description".
- **`views/JobsView.vue:796,805`** — the Service Calls count tiles are keyed on
  the `"Service Call"` literal and **undercount by 12**.
- `core/ai_quote.py` / `core/ai_router.py:547` match quote templates on
  `job_type` equality — template lookups miss on the wrong spelling.

### Fix — DECIDED (Doug 2026-07-29)

**Canonical vocabulary is `job_type` itself; the pricing lane is a separate
function of it.** Keeping `Repair` and `Maintenance` as distinct stored values
preserves information the office finds useful, while the lane resolver decides
how they price. Don't collapse descriptors into lanes at write time.

One shared list in `core/job_taxonomy.py`, imported by **both** dropdowns:

| canonical `job_type` | pricing lane |
| --- | --- |
| `Service Call` | **service — hourly** (first-hour min, $100/hr, round up to 0.5) |
| `Repair` | **service — hourly** ✅ Doug 2026-07-29 |
| `Maintenance` | **service — hourly** ✅ Doug 2026-07-29 |
| `Installation` | **install — flat** from `labor_price_items` |
| `New Construction` | office-priced (unresolved — see below) |
| `Inspection` | office-priced (unresolved) |
| `Other` | office-priced |
| `QB Import` (159 legacy) | office-priced, never auto-priced |
| NULL / anything unrecognized | office-priced |

**Canonical service spelling is `Service Call`** — it's the 34-row majority, it's
what the dedicated `service_calls.py` reader and the primary Jobs dropdown
already use, and it's the term the business uses ("service call hourly rate").

Steps:

1. `core/job_taxonomy.py`: the canonical list, a case-insensitive normalizer for
   legacy aliases (`Service`/`service` → `Service Call`;
   `Install`/`installation` → `Installation`), and a `pricing_lane(job_type)`
   function returning `service` / `install` / `office`.
2. Every writer uses the constants — including `instant_estimate.py` (currently
   lowercase) and `seed_demo.py` (currently `"Install"`), which are latent
   producers of two more spellings.
3. **Backfill the 12 `Service` rows → `Service Call`.** ✅ approved. `Repair` and
   `Maintenance` need no backfill — zero rows exist today; they only need to be
   in the list and mapped.
4. The four broken readers call the helper instead of comparing literals:
   `service_calls.py:117`, `JobsView.vue:658`, the count tiles at
   `JobsView.vue:796,805`, and the `ai_quote.py`/`ai_router.py` template match.
5. CHECK constraint last, so free text can't reappear — it's the step that turns
   a silent wrong answer into a loud one.

**Still unresolved, and safe by default:** `New Construction` and `Inspection`
stay office-priced until you say otherwise. Zero rows exist for either, so
nothing is blocked, and "office prices it" can never bill a customer wrong. Each
is a one-line change to the table above whenever you decide — `New Construction`
most likely belongs with installs.

### `QB Import` is a separate, deliberate-ish thing — leave it alone for now

Those 159 rows use `job_type` to record **provenance**, not work kind. That's a
category error in the same field, but it is how imported jobs are currently
recognized, and 159 rows of history depend on it. Treating them as "unknown work
kind → office prices it" (§8) is the safe behaviour and needs no change. The
long-term cleanup is a real `source` field, tracked separately from this plan.

### Why this matters for §8

Without canonicalization, the billing code would need its own defensive
normalizer at the read site — and every future reader would repeat the same bug.
Fixing the column once means the pricing lane can trust `job_type`. This should
land **before or with** §8, not after.

---

## 10. Adversarial audit of this plan (2026-07-29)

Requested by Doug after the plan was drafted. Every finding below was checked
against prod or against the code as it stands, not reasoned about abstractly.
Two of them falsified load-bearing claims I had written confidently.

### A1 — FOUNDATIONAL. "Techs can already invoice" was false for ~90% of jobs

**Severity: high. Blocks §8 entirely.**

I claimed no permission work was needed because the mobile invoice endpoint gates
only on the `mobile` module plus `_job_belongs_to_tech`. The *role* is indeed
fine. The **gate is broken**.

`mobile_invoicing._job_belongs_to_tech` (`routers/mobile_invoicing.py:83-95`)
checks two things. The first:

```sql
SELECT 1 FROM jobs WHERE id = :jid AND deleted_at IS NULL AND assigned_to = :uid
```

passes `:uid` = the **user** id. In prod, `jobs.assigned_to` holds a
**technician** id in **22 of 22** non-empty rows and a user id in **0**. That
comparison can never be true. Ownership therefore rests entirely on the second
check, the `job_assignments` join — and only **19 of 190** completed jobs have a
`job_assignments` row.

So field billing would 404 for the tech on the large majority of jobs.
JOB-2026-006 works only because it happens to have an assignment row — I
validated the design against the one job that hides the bug.

**The kicker: the correct implementation already exists.**
`core/job_access.job_belongs_to_user` documents this exact trap in its docstring
("CRITICAL: jobs.assigned_to stores a *technician.id*, not a users.id") and
handles four ownership paths including the technician mapping and an appointment
fallback. It is the audited write gate for ~18 mobile endpoints.

**Fix:** delete `_job_belongs_to_tech` and call `job_belongs_to_user`. One gate,
already hardened. Do **not** patch the duplicate — two implementations that
disagree is how this happened.

### A2 — "Tax should come back 0" was false, and my advice would have started charging it

**Severity: high. Money, customer-facing.**

I wrote that tax should be resolved via the shared resolver and that MN
construction-contract rules mean "this should come back 0."

Prod `tax_config.default_rate` = **0.0738**, and `modules/tax/service.resolve_rate`
returns 0 **only** when the customer is explicitly flagged exempt — otherwise it
returns the default. The mobile path today sets tax to 0 by omission. So
"resolve tax properly" would have **added 7.38% to every field-billed invoice**,
introducing a customer-facing charge where there is none today, on exactly the
work the standing MN rule says shouldn't carry customer sales tax.

Worth surfacing separately, because it is already live: GDX-generated invoices
(`INV-%`) number 24, of which **13 carry tax totalling $3,208.75** — most
recently INV-000331 dated 2026-07-29. (The other 300 invoices are QB imports
carrying $29,750.35 of historical tax from QuickBooks, which is not GDX's doing.)
There is also an internal inconsistency worth a look: `tax_config.tax_labor` is
`false`, and INV-000328 taxed only part of its subtotal accordingly — but
INV-000331 taxed the full $600.00 at 7.38% ($44.28), labor included.

**Fix:** this plan changes **no** tax behaviour. Keep the mobile path at 0. The
question of whether those 13 invoices are correct is an accountant call for you,
not something to settle inside a billing feature. Flagging, not fixing.

### A3 — Ordering bug in my own §5: the unique index would fail the migration

**Severity: medium. Self-inflicted.**

§5 listed "partial unique index … ; backfill … ; renumber the five collisions."
A partial unique index **cannot be created while the five duplicates exist**, so
in the order written the migration fails on the way in. Corrected in §5: dedupe
and backfill first, index last.

### A4 — My §1 closeout endpoint would have leaked customer signatures

**Severity: medium. Bypasses an existing audited control.**

I specified the closeout GET as "same read gate as job detail" returning the
snapshot. But `routers/mobile.py:2053-2058` deliberately nulls `signature_data`
for a tech browsing under `techs_see_all_jobs`, per an /audit finding of
2026-07-22. `job_closeouts.signature_data` holds the same blob, so a new endpoint
returning the snapshot verbatim walks around that control. Corrected in §1.

### A5 — Hours become a customer price with no office review before send

**Severity: high. Design gap, not a code bug.**

Under flat-rate pricing, a mistyped hours figure cost nothing but a bad costing
number. On the hourly lane it **is the invoice**. And per Doug's own account,
JOB-2026-006's 3.00 was typed from memory after the fact — which is the normal
case, not an edge case.

Nothing in the plan stops a tech creating **and sending** an invoice from the
truck: `POST /api/mobile/invoices/{invoice_id}/send` already exists. A fat-fingered
8 instead of 3 mails an $800 invoice to a customer with no second pair of eyes.

**Fix:** on the hourly lane, the tech may create the invoice but **not send it**
until the office has seen the hours. This is a deliberate asymmetry with the
install lane, where the price comes from the price book rather than from a typed
number. Doug's call whether the office review is a hard gate or a warning.

### A6 — Multi-tech and multi-visit jobs will underbill

**Severity: high. Revenue, silent.**

`hours_worked` is a **single number per job** (one `JobCloseout`, re-closeout
restates it), and closeout deliberately closes every *other* open timer on the
job at **zero** hours. So on a two-tech job, tech A attests 3h, tech B's timer
closes at 0, and the hourly lane bills **3h — for 6 man-hours of work.**

That the domain cares about this is visible in the schema: `labor_price_items`
carries `default_crew_size` and `assumed_man_hours`. The flat lane absorbs crew
size in the price; the hourly lane has nowhere to put it.

Same shape for a job spanning two visits: one closeout row, so either the tech
restates a running total or the second visit's time vanishes.

**Unresolved — needs a decision before §8 ships.** Options: capture hours
per-tech at closeout; multiply by crew size; or restrict the hourly lane to
single-tech jobs and route multi-tech ones to office-priced. I'd recommend
per-tech capture, but it is a real scope increase and it is better to know that
now than after the first underbilled two-man service call.

### A7 — Re-closeout after invoicing has undefined behaviour for labor

**Severity: medium.**

The closeout code is careful about this for *parts*: a re-closeout replaces
still-unbilled closeout rows and never touches billed ones. For **labor** the
plan says nothing. So: tech closes out at 3h, an invoice is created, tech
re-closes out at 4h. The `JobCloseout` and the `time_entries` row are restated,
the issued invoice is not, and nothing reconciles them.

**Fix:** mirror the parts rule — once a labor line has been billed, a re-closeout
must not silently restate the billed quantity; surface the discrepancy to the
office instead. Consistent with the standing "discrepancies go to the dispatcher,
never block the tech" rule.

### A8 — §4's fix turns on a feature that has never once run in prod

**Severity: medium. Unknown blast radius.**

I framed §4 as "build a correct appointment payload." But that POST has **422'd
every time it has ever been called**, so no job-create has ever produced an
appointment. Fixing the payload doesn't restore behaviour — it **switches on**
appointment creation for the first time, and appointments feed the dispatch
board, `job_belongs_to_user`'s ownership path (!), and arrival SMS.

Note the interaction with A1: appointments are one of the four ownership paths,
so creating them would incidentally *fix* some ownership 404s — which is good,
but means the two changes interact and shouldn't be reasoned about separately.

**Fix:** the error-handling half of §4 (dialog closes, list refreshes, job never
looks unsaved) is the urgent, safe part and should ship on its own. Actually
creating appointments is a separate change that needs its own verification pass
against the dispatch board and notifications.

### What survived

- §1's core finding (closeout is write-only, only reader is tech_efficiency) —
  re-verified, no GET route exists.
- §9's `job_type` drift, the two dropdowns, and the four broken readers.
- §6's two `tenant_settings` rows and the `.first()` coin flip.
- §3's blank Tech column, the `body`-as-prop bug, and the $65-vs-$50-vs-$95
  cost-rate split.
- §7's honest "did not reproduce" — still the right call; no paid-but-queued job
  exists in current data.
- The decision to price installs flat and service hourly, and to keep attested
  hours separate from billed hours.

### Revised risk order

A1 and A5/A6 mean **§8 is not ready to build as written.** A1 is a small fix to a
real blocker; A5, A6 and A7 are product decisions about what an hourly invoice
means when hours are typed from memory, crews vary, and closeouts get redone.
Those want answers before code.

---

## 11. Office verification + closeout prompts — resolves A5 and A6

**Doug 2026-07-29:** "have the office be called to verify the invoice. And have it
ask is this how many hours you meant? And have it ask how many techs on site."

### 11.1 Closeout asks how many techs were on site

New column `job_closeouts.techs_on_site` (int, NOT NULL, default 1, min 1) plus a
field in the closeout dialog.

**The semantics have to be written down, because A6 exists precisely because they
were ambiguous:**

- `hours_worked` = **on-site duration**, not man-hours.
- `techs_on_site` = crew size for that duration.
- **billed man-hours = `roundup_half(hours_worked) × techs_on_site`**, then the
  1-hour floor.

So that job, if it had been a 2-man call: 3.0 hrs × 2 = 6.0 man-hours →
$600, instead of the $300 the plan would have billed before this change.

> **⚠ Guardrail — crew size feeds BILLING ONLY.** `techs_on_site` must never
> create payable hours for the other techs. Their pay comes from their own day
> clock; closeout still closes their per-job timers at zero, unattested. Turning
> "2 techs" into two payroll rows would be the labor code inventing hours for a
> person who never attested — the exact failure the standing rule forbids. One
> tech attesting "there were two of us for 3 hours" is a billing fact, not a
> payroll attestation for his colleague.

Residual case, accepted: if the second tech was only there part of the time, the
multiply overbills. The office verification step (11.3) is where that gets
caught — which is a large part of why 11.3 is worth having.

### 11.2 Closeout confirms the hours by showing the money

The confirmation must show the **consequence**, not echo the input — a bare "are
you sure?" gets tapped through reflexively, and this prompt is the control that
makes a typed-from-memory number trustworthy enough to bill:

```text
You entered 3.0 hours with 2 techs on site.

That bills 6.0 man-hours at $100/hr = $600.00

   [ Yes, that's right ]    [ Let me change it ]
```

Record that the tech confirmed (`hours_confirmed_at` on the closeout). Cheap, and
it distinguishes "the tech saw this number and stood behind it" from "the prompt
never rendered" — which matters for exactly the reason below.

**Verified, because it would have made this prompt theater:** the repo has a
known issue (#215) that `useDestructiveConfirm` can auto-accept without showing
anything. I checked the real app rather than trusting the note:
`ConfirmationService` **is** registered (`main.js:54`) and `<ConfirmDialog>` is
mounted by `AppLayout`, which wraps every route not flagged `noShell` — and no
`/mobile*` route is. So confirms **do** render for the tech. The auto-accept
branch (`useDestructiveConfirm.js:61-66`) only fires when the service is absent,
i.e. in unit tests that skip `app.use()`. Use the standard helper; do **not** add
a bespoke confirm. But pin it with a test that asserts the dialog renders on the
mobile route, because if that ever regresses, this control fails **open** and the
invoice bills whatever was typed.

### 11.3 The office verifies before anything reaches a customer

Every **tech-created** invoice lands as `draft` + awaiting office verification,
and **the tech cannot send it.**

- Add `verified_at` + `verified_by_user_id` to `Invoice`.
- `POST /api/mobile/invoices/{id}/send` refuses (409) while `verified_at` is
  NULL. This is the load-bearing half — that endpoint exists today and is what
  makes A5 possible.
- The office queue shows the closeout's hours, `techs_on_site`, the computed
  man-hours and the total — **all editable** — then a Verify action that stamps
  `verified_at` and unlocks sending.
- Keep `verified_at` distinct from `sent_at`. `sent_at` is already defined as a
  delivery fact, not an attempt or an approval; don't overload it.

**Applies to both lanes, deliberately.** The hourly lane is the risky one, but a
uniform rule is simpler for the tech than "sometimes you can send, sometimes you
can't," and the flat lane still benefits from someone checking the right service
was picked. Say the word if you'd rather let install invoices go straight out.

On "the office be called to verify" — I read that as *the office is required to
verify*, implemented as the gate above. If you literally meant the tech should be
prompted to **phone** the office, that's a one-line change to the closeout
confirmation text and no backend gate; tell me which.

### Consequences for §10

- **A5 resolved** — office verification gate + a consequence-showing confirmation.
- **A6 resolved** — `techs_on_site` captured, semantics pinned, billing-only.
- **A7 still open** — re-closeout after invoicing. The verification gate helps
  (an unsent invoice can be corrected), but a *sent* invoice whose closeout is
  later restated still needs a rule.

---

## 12. A7 escalated — the closeout snapshot is append-only, and everything downstream diverges

**Doug 2026-07-29: "a7 that is a problem in any scenario."** Correct, and chasing
it found a code defect I had missed. A7 is promoted from "open question about sent
invoices" to a blocker with a verified root cause.

### The defect

`closeout_job` **never replaces an existing snapshot.** It unconditionally
constructs `JobCloseout(id=uuid.uuid4(), …)` and `db.add`s it
(`routers/jobs.py:1879`). There is no UPDATE, no soft-delete of the prior row, no
upsert, and no unique constraint on `job_id`. So a second closeout of the same job
leaves **two live rows**, a third leaves three.

The comment at `routers/jobs.py:1527` that reads "one JobCloseout per job,
re-closeout restates it" is describing `_owned_closeout_labor_entry` — the *labor
entry* helper, which genuinely is idempotent. The **snapshot** is append-only. I
read that comment as covering both. It doesn't.

**Prod has not hit it yet:** 8 closeouts across 8 distinct jobs, zero duplicates —
nobody has re-closed out a job. It fires the first time anyone does, and §1 and §8
were both about to be built on top of it.

### Why Doug is right that it's a problem in every scenario

| Invoice state | What breaks |
| --- | --- |
| **No invoice yet** | Two live snapshots, no marker for which is current. Any reader picks arbitrarily. |
| **Draft, unverified** | The office verifies an invoice built from the *old* hours while the closeout now says something else — and **nothing surfaces the change**. My §11 claim that "an unsent invoice can just be corrected" assumed the office would notice. It won't. The gate becomes a rubber stamp. |
| **Sent** | The customer holds a $300 bill; the records say 4h/$400. Permanent divergence. |
| **Paid** | Money reconciled against a number that has since changed. |

And two readers are already wrong the moment it happens, before any of this
plan's new code exists:

- **`routers/tech_efficiency.py:79`** joins `job_closeouts` — two rows for one job
  **double-counts hours** in the efficiency ratio.
- **My planned `GET /api/jobs/{job_id}/closeout`** would fetch "the" closeout. The
  natural `scalar_one_or_none()` raises `MultipleResultsFound` → a **500 on the
  office card**, precisely on the jobs that were redone.

There's a third, quieter one: the labor `time_entries` row *is* mutated in place by
`_close_labor_entry`, so a re-closeout **overwrites the attested hours** there.
That is the same "never overwrite an attestation" problem in a different table.

### Fix — supersede, never overwrite; and make derived records follow or refuse

1. **Append-only history with exactly one current row.** Add `superseded_at` +
   `supersedes_id` to `job_closeouts`; a re-closeout marks the prior row superseded
   and links to it. Keep every row — an attestation is evidence and must never be
   destroyed, only dated. Add a partial unique index: one live row per `job_id`.
2. **Every reader takes the current row explicitly**, via one shared helper.
   **Fix `tech_efficiency.py` as part of this** — it is already wrong, independent
   of billing.
3. **Stop mutating the billed labor entry.** Mirror the rule parts already follow:
   an unbilled labor row may be restated; a **billed** one is superseded, not
   rewritten.
4. **Derived invoices follow the change, by state:**
   - *No invoice* → record the history, nothing else to do.
   - *Draft + unverified* → **clear `verified_at` and flag the invoice**
     "closeout changed after this invoice was built — re-check." This is the piece
     I got wrong in §11: the office must be *told*, not merely *able*.
   - *Sent or paid* → **never mutate silently.** Raise a discrepancy to the
     office and require an explicit correction. Consistent with the standing rule:
     discrepancies go to the office, never block the tech in the field.
5. **Guard the write path:** a re-closeout on a job whose invoice is already sent
   should warn the tech that the office will have to handle it — informational, not
   a block.

### Correction route — DECIDED: adjustment / credit, never void-and-reissue

Doug 2026-07-29. The original document always stands; a second document carries
the difference. Good news: **most of this already exists and is UI-wired.**

- `POST /api/invoices/{invoice_id}/credit-memo` (`routers/invoices.py:2218`)
  writes an `InvoiceAdjustment(kind="credit_memo")`, posts the GL entry via
  `post_credit_memo`, resettles payments, recalculates the invoice, and audits.
  Reachable from `InvoiceDetailView.vue:372`.
- `POST /api/invoices/{invoice_id}/refund` (`routers/invoices.py:2366`) writes
  `kind="refund"`. Reachable from `PaymentsView.vue:400`.

**But a credit memo only goes DOWN, is capped at the remaining balance, and
rejects drafts.** So "just issue a credit memo" is wrong in three of four cases:

| Hours change | Invoice state | Correct mechanism | Exists? |
| --- | --- | --- | --- |
| Down (overbilled) | sent/overdue, balance left | **Credit memo** on the original | ✅ wired |
| Down (overbilled) | **fully paid**, balance 0 | Credit memo **422s** — "exceeds the remaining balance (0.00)". Needs a **refund** | ✅ wired, but money-out |
| **Up** (underbilled) | sent or paid | **Supplemental invoice for the difference** | ❌ **must build** |
| Either | **draft** | Credit memo **409s** by design ("drafts are edited, not credited"). Just edit the draft and clear `verified_at` | ✅ per §12 above |

Implementation notes for the one new piece:

1. **The supplemental invoice is a normal invoice for the delta**, not a new
   document type. Give it `billing_type='standard'`.
2. **Do NOT add an `adjustment` value to the `billing_type` enum.** That's a
   Postgres `ADD VALUE` inside a migration that runs in a single transaction
   (`migrations/env.py`) — a known hazard in this repo. Add a **nullable
   `adjusts_invoice_id` FK column** instead (a plain column add) so both
   directions are traceable to the invoice they correct, and stamp it on the
   credit-memo path too.
3. **It must not re-bill parts.** Reuse the existing `billed_invoice_id` stamp
   rule — parts already claimed by the original invoice are excluded.
4. `job_billed_exists()` treats a `standard`, non-void, total>0 invoice as
   billing the job, which is the correct outcome here. Just don't let it near
   `billing_type='deposit'`, or the §7 carve-out will misread it.
5. **Office-only, all of it.** The tech's re-closeout raises a discrepancy; the
   office decides credit vs refund vs supplemental. Note the refund path needs
   `invoices.refund`, which `accounting` holds and `dispatcher` does **not** — so
   the paid-and-overbilled case may need an accounting user, not just whoever is
   at the front desk. Worth confirming that's the intent.

> The 2026-07-24 contract sweep noted "unreachable A/R credits". I checked rather
> than assuming: **credit-memo and refund are both reachable** from
> `InvoiceDetailView` and `PaymentsView` respectively. Whatever that note refers
> to, it isn't these two endpoints.

---

## 13. Audit trail — what exists, and what this plan must add

**Doug asked: "do we have a full audit trail built into this?"** Checked against
prod rather than assumed. Short answer: **the infrastructure is genuinely strong,
the coverage has one live hole, the integrity check is never run, and the new
paths in this plan have no events specified yet.**

### What's actually there (verified, prod)

- **Hash-chained and tamper-evident.** `audit_logs` carries `prev_hash` +
  `row_hash`, and `core/audit.verify_audit_chain` recomputes the chain over
  `tenant:actor:action:entity_type:entity_id:details:request_id`.
- **18,315 rows, and the integrity columns are complete:** 0 rows missing a hash,
  0 missing `prev_hash`, **0 missing an actor.** The activity-attribution work has
  fully landed — the "85% of rows had no actor" problem is gone.
- Query surface exists: `/api/audit/logs`, `/logs/export`, `/entity/{type}/{id}`,
  `/user/{id}`.
- Closeout **is** audited today (`action="job_closeout"`, details carry
  `closeout_id`, `parts_count`, `hours`, `signature_present`).

### Gap 1 — closeout audit coverage starts 2026-07-17

8 closeout rows exist; only 4 have a `job_closeout` audit event. Not a silent
failure — it's a clean cutoff:

| Closed | Audit events |
| --- | --- |
| 2026-05-10, 05-10, 05-21, 06-01 | **0** |
| 2026-07-17, 07-29, 07-29, 07-29 | 1 each |

So instrumentation was added mid-July and every closeout since is audited 1:1.
The four early ones are pre-instrumentation history and can't be recovered.
Worth knowing before anyone treats the trail as complete back to May.

### Gap 2 — nothing ever verifies the chain

`verify_audit_chain` is referenced **only from tests**
(`test_audit_compliance.py`, `test_29_audit_dashboard.py`). `routers/audit.py`
exposes `/logs`, `/logs/export`, `/entity/...`, `/user/...` — and **no
verify endpoint**. There's no scheduled check either. So the tamper-evidence is
real but unexercised: a broken chain would go unnoticed indefinitely.

**Fix (cheap, worth doing while we're in here):** an admin-only
`GET /api/audit/verify-chain` plus a periodic task that logs the result. A
tamper-evident log nobody checks is a filing cabinet with a good lock and no one
holding the key.

### Gap 3 — corroboration for A1, incidentally

`mobile_invoice_created` has **zero** audit rows, ever. Combined with the broken
ownership gate (A1), that's independent evidence field billing has never
successfully run in prod — not "rarely used", *never*. Same for
`credit_memo_issued` (0) and `create_job_time_entry` (0).

### What this plan MUST add

The governing requirement: **the invoice amount must be reconstructible from the
audit trail alone.** Today's `job_closeout` details record `hours` but nothing
about how that became money. Required additions:

| Event | Details it must carry | Why |
| --- | --- | --- |
| `job_closeout` (extend) | `techs_on_site`, `hours_confirmed_at`, billed man-hours, pricing lane (`service`/`install`/`office`), the rate or matrix row used | Without the lane and rate, a $600 invoice can't be explained later |
| `job_closeout_superseded` (new) | `old_closeout_id`, `new_closeout_id`, **old vs new** hours and techs | §12's whole point. Today's events record only the *new* value, so a change is reconstructible solely by diffing two events — record the delta explicitly |
| `invoice_verified` (new) | `invoice_id`, hours, techs, man-hours, total, `verified_by` | §11's accountability record: who approved this going to a customer |
| `invoice_verification_cleared` (new) | `invoice_id`, reason (`closeout_changed`), triggering closeout id | So a re-verification is visibly a *second* approval, not the first |
| `supplemental_invoice_created` (new) | `original_invoice_id`, `adjusts_invoice_id`, delta, reason | §12 up-direction |
| `credit_memo_issued` (extend) | add `adjusts_invoice_id` | Both correction directions traceable the same way |

Two existing weak spots to fix in passing:

- **`create_job_time_entry` logs `details={}`** (`routers/labor.py`) — an empty
  dict. Hours and rate changes leave no record of *what* changed. Fill it in as
  part of §3.
- **`_close_labor_entry` mutates hours in place** with no per-row event. §12
  already requires superseding a billed row; the audit event goes with it.

### Backfills must not bypass the trail

§5 (renumber 5 jobs, backfill 186 numbers) and §9 (rebrand 12 `job_type` rows)
are **office-visible data mutations performed by a script**, and a migration
doesn't call `log_audit_event_sync`. As written they'd leave no trace.

**Requirement:** every backfill writes a batch audit event recording what changed
(counts, old→new mapping) **and** saves a before/after CSV alongside the
migration. If the office later asks "why is this job numbered differently", the
answer has to exist somewhere.

---

## 14. UI coverage — desktop and mobile

**Doug asked: "does everything have a UI? mobile and desktop"** Checked each
surface. Answer: **no — six things had no UI specified at all, and one of the gaps
costs real money.** One piece of good news makes several others cheap.

### The good news: the closeout dialog is already shared

`MobileJobCloseoutDialog` is consumed by **three** views — `MobileTodayView`,
`MobileJobDetailView`, and **`DispatchView.vue:600` (desktop)**. So every field
§8 and §11 add — service picker, `techs_on_site`, the hours confirmation — lands
on **both** surfaces from one component.

Two caveats: a component named `Mobile*` is doing double duty on the desktop
dispatch board, so anything added must be laid out for both widths (not just
390 px); and the dispatcher closing out on desktop gets the same prompts as the
tech, which is correct — they're attesting the same facts.

### Gap 1 — MONEY. Mobile job-create cannot set `job_type`

`MobileJobNewDialog`'s payload (`components/MobileJobNewDialog.vue:285-294`) is
`title`, `description`, `customer_id`, `scheduled_duration_hours`, `location_id`.
**There is no `job_type` field in the dialog at all**, so every mobile-created job
takes the backend default `"Service"` (`routers/jobs.py:72`).

Consequences, and the second one is expensive:

1. Today: those jobs get the non-canonical `Service` spelling — so they're already
   invisible to the service-call queue (§9's broken reader).
2. After §8: `Service` normalizes into the **hourly lane**, and a tech creating an
   **install** from his phone has no way to say so. A 16x7 sectional install that
   should bill its $650 flat price would instead bill 5 hrs × $100 = **$500. A
   $150 underbill, silently, every time.**

**Fix:** add the shared `job_type` picker (§9's canonical list) to
`MobileJobNewDialog`. This is a prerequisite for §8, not a nicety.

### Gap 2 — the §11 office verification queue has no home

The whole verification gate is backend-only as written. It needs a real desktop
surface: a queue of invoices awaiting verification, each showing hours,
`techs_on_site`, computed man-hours and total, **all editable**, with a Verify
action. Natural home is `BillingView` (alongside Ready-for-Billing) with the
per-invoice detail on `InvoiceDetailView`. Desktop only — verification is an
office act.

### Gap 3 — the tech can't see that his invoice is waiting

This one repeats the exact failure mode of §4. If a tech creates an invoice and
the app shows nothing, he concludes it didn't work and tries again. He needs an
explicit **"sent to office for verification"** state on the mobile job/billing
screen, and the Send button visibly disabled with the reason — not silently
absent. Mobile only.

### Gaps 4–7 — smaller, all desktop

| Gap | Surface | Note |
| --- | --- | --- |
| Closeout history (superseded rows) | `JobDetailView` closeout card | §12 keeps every attestation; the office needs to see "revised from 3.0 → 4.0 on DATE by PERSON". Without it the supersede model is invisible |
| Discrepancy alert (closeout changed after billing) | Office queue / `InvoiceDetailView` | §12's "raise it to the office" needs somewhere to land, or it raises into nothing |
| Supplemental invoice action | `InvoiceDetailView` | Credit-memo and apply-credit buttons already live at `InvoiceDetailView.vue:372,381`; the up-direction correction needs a sibling |
| Audit chain verify | `AuditLogViewer.vue` | §13's new endpoint needs a button and a result |

### Deliberately no UI

§5 renumbering/backfill, §6 `tenant_settings` reconcile, §7.3 predicate fix, §9's
12-row backfill — all data or logic fixes. They get audit batch records (§13)
rather than screens. §3's time-entry table is desktop-only on purpose: the tech
sees his hours through §1's closeout card, and the labor endpoint is
dispatch-gated.

### One thing to check while in §4

`MobileJobNewDialog` wraps its job-create POST in a `try/catch` and its
parts-needed loop separately, so it may not have JobsView's unguarded-side-call
bug — but its dialog-close and list-refresh path should be confirmed against the
same failure, since the symptom Doug reported ("creating a job and saving it")
could come from either surface.

### Verification standard for all of the above

Per the house rule: real browser, both **light and dark**, desktop **and** 390 px,
plus the Pixel 8 emulator for anything the tech touches in the field — the
closeout dialog's new fields especially, since that component now renders on two
very different widths.

---

## 15. Estimate provenance and win/loss — corrects §8, adds a third lane

**Doug 2026-07-29: "This scenario is from an estimate. And the win or loss should
be tracked somehow."** Both halves land. The first one **corrects §8's pricing
model**; the second exposes $131k of pipeline that never resolves.

### 15.1 The estimate lane comes FIRST — §8 corrected

Per the domain rule, an install *is* a converted estimate. So for that work the
price was already agreed with the customer, and neither the matrix nor an hourly
rate should decide it. Revised precedence:

| # | Condition | Price source |
| --- | --- | --- |
| **1** | Job has an **accepted estimate** | **The estimate** — copy its lines |
| 2 | else `job_type` = `Installation` | Matrix `flat_price` (**fallback**, for work priced on the spot) |
| 3 | else service lane (`Service Call`/`Repair`/`Maintenance`) | First-hour min, then hourly @ $100 |
| 4 | else | Office-priced |

This is mostly **already built**. `mobile_create_invoice` copies estimate lines
today — both the proposal-tier path (single summary line for the accepted tier)
and the plain path (all lines verbatim) — and it already honours
`hide_line_prices` so the invoice matches what the customer signed, and already
runs `apply_deposits_to_final`. Desktop has the equivalent in
`prefillFromJobEstimate`. §8's job is to make the estimate lane the **default**
rather than something the caller has to pass an `estimate_id` for.

**The link is `Estimate.job_id`** — `Job` has no `estimate_id` (only `Invoice`
does, at `models/tenant_models.py:407`). So from a job, the accepted estimate is
`Estimate WHERE job_id = job.id AND status = 'accepted'`. Worth considering adding
`Job.estimate_id` so the chain reads forward, but the reverse lookup works today.

> **Correction to §14 Gap 1.** I said a mobile-created install would underbill
> "$150, every time." With the estimate lane taking precedence that's too strong:
> it only mis-lanes an install created on the phone **without** an accepted
> estimate. Still a §8 prerequisite — but the failure is narrower than I stated.

Note the §11 verification gate still applies uniformly, though the rationale
differs: an estimate-sourced invoice carries a customer-agreed price and no typed
hours, so the A5 risk isn't present. Keeping it uniform is for the tech's sake
(one consistent rule), not because the risk is equal.

### 15.2 Win/loss — what exists

More than I expected:

- Statuses: `draft`, `sent`, `accepted`, `declined`, `rejected`, `expired`, plus
  **`declined_at` and `declined_reason` columns**.
- `reports.py:837` computes `close_rate` over **decided** estimates
  (`accepted|declined|rejected|expired`) and — to its credit — tracks
  **outstanding** sent estimates separately, aged from `sent_at`.
- `GET /api/estimates/analytics/conversion-rate` reports conversion **by
  `job_type`**.

### 15.3 The gaps (all verified in prod)

**Gap A — we never record why we lost.** `declined_reason` is populated **0 times
out of 4** declined estimates, even though `declined_at` is set on all 4. The
column exists and nothing fills it. Make a reason **required** on decline, from a
short picker (price / timing / went elsewhere / scope changed / no response) plus
free text.

**Gap B — losses hide in `sent` forever.** Prod:

| Status | Count | Value |
| --- | --- | --- |
| sent | **35** | **$236,609** |
| accepted | 11 | $78,034 |
| declined | 4 | $46,406 |
| draft | 4 | $10,869 |

Of the 35 sent, **16 are older than 30 days, worth $131,026** — and **zero
estimates have ever reached `expired`.** Nothing ages them out. So the close rate
is computed over 15 decided estimates (11 won / 4 lost ≈ 73%) while $131k of
probable losses never enters the denominator at all. The metric isn't lying; the
**data never resolves**.

Fix: an aging process that flips stale `sent` → `expired` (needs a window from
Doug — 30/60/90 days), plus an explicit **"mark won / mark lost"** action on the
estimate so the office can resolve one without waiting for a timer.

**Gap C — `declined` *and* `rejected` are the same concept.** Exactly the §9
`job_type` drift in a different column. Prod uses only `declined`; `rejected` is
dead but reachable, so any reader comparing against one literal silently misses
the other. `reports.py` happens to handle all four — other readers may not. Pick
one, normalize, and fold it into §9's canonicalization work.

**Gap D — the conversion report inherits §9's drift.** `conversion-rate` groups by
`job_type`, so `Service` and `Service Call` split one work kind into two rows, and
jobs with no estimate land under `"Unknown"`. **Fixing §9 fixes this report too** —
worth noting so the win/loss numbers are trusted afterward.

### 15.4 Why this belongs in this plan

The estimate is where the money is **decided**; the closeout is where it's
**confirmed**. This plan already covers closeout → invoice; §15 closes the front
of the same chain, so `estimate → job → closeout → invoice` is traceable end to
end, with an outcome recorded at the point the customer said yes or no. Every
audit requirement in §13 applies: a status change to won/lost, and the reason,
are events.

**DECIDED (Doug 2026-07-29): loss reason is MANDATORY on decline** — short picker
(price / timing / went elsewhere / scope changed / no response) + optional free
text; the API rejects a decline without one.

### 15.5 Expiry window — DECIDED: company setting, default 60 days

Doug 2026-07-29. There's a clean existing pattern to follow, and one trap in it.

**The setting** — `estimate_expiry_days` on `control/models.py` `TenantSettings`
(`Integer, nullable=False, default=60, server_default="60"`), exposed through the
**existing** `modules/estimates_features` module: add it to `_COLS`, to
`FeaturesPayload` (`estimate_expiry_days: int = 60`), and to `int_cols`. That
module's `_read` already keys correctly on `WHERE tenant_id = :tid` and self-heals
a missing row with `INSERT … ON CONFLICT DO NOTHING`.

> ⚠ **Trap in that module.** The int branch is
> `out[col] = int(val if val is not None else 50)` — a **single hard-coded 50** for
> every int column, because `estimate_deposit_pct` (50%) is currently the only one.
> Adding a second int column makes **50 the fallback for the expiry window too**.
> Make the fallback per-column before adding the field, or a tenant with a NULL
> lands on a 50-day expiry while the model claims 60.

**The UI** — `SettingsView.vue` already has an Estimate Settings section wired to
this module (GET at `:1767`, PATCH at `:1774`, save button at `:594`). The field
goes there. Desktop only; it's an office policy setting, so no mobile surface.

Deliberately **not** following the precedent of `estimate_draft_archive_days`
(also 60 days, same table): that one has a nightly task and **no UI anywhere** — a
policy nobody can change. Doug asked for a company setting, so this one gets a
control.

**The task** — mirror `tasks/estimate_archive.py`: nightly, `queue="priority:low"`,
registered in `core/celery_app.py` and `core/scheduler.py`, flipping `sent`
estimates older than the window to `expired` and logging a structured count.

> ⚠ **Do not copy its settings read.** `tasks/estimate_archive.py:134` does
> `SELECT estimate_draft_archive_days FROM tenant_settings LIMIT 1` — the **same
> arbitrary-row bug as §6**. With two `tenant_settings` rows in prod that's benign
> only because both currently hold 60. The new task must key on `tenant_id`, and
> §6's reconcile matters here directly: an expiry window is a policy that must not
> depend on which row Postgres happens to return first.

**Backfill question for Doug:** switching this on will immediately expire the 16
`sent` estimates already older than 60 days (**$131,026**). That's the correct
outcome — they're stale — but it's a one-time visible swing in the close rate as
$131k of hidden losses land in the denominator at once. Better to expect it than to
discover it. Worth doing as a reviewed one-off list rather than letting the nightly
task sweep them silently on first run.

### 15.6 Expiry anchors on `valid_until` — and reopening is easy

**Doug: "if they are expired can we reopen them? sometimes people come back months
later and the numbers are still good."** Yes — and asking it surfaced that an
expiry feature already exists, dead in four separate layers.

#### What's already there, and why nothing has ever expired

`Estimate.valid_until` exists, and `POST /api/estimates/expire-stale`
(`routers/estimates.py:2117`) marks estimates expired once past it. It has never
fired once, for four independent reasons:

1. **`valid_until` is NULL on all 54 estimates** — every status, zero populated.
   The sweeper filters `valid_until IS NOT NULL`, so it can never match a row.
2. **`_serialize_estimate` never returns it** (`routers/estimates.py:156-182`) —
   accepted on input at `:406`, never sent back out.
3. **`EstimateView.vue:33` renders `estimate.expires_at`** — a field name that
   doesn't exist on the API or the model (which calls it `valid_until`). So the
   "Expires:" line on the estimate screen has always been blank.
4. **Nothing calls `expire-stale`** — not in `scheduler.py`, not in
   `celery_app.py`, no frontend reference.

#### Revision to §15.5: `valid_until` is the authority, the setting is the default

Better than a tenant-wide day count computed from `sent_at`:

- **`valid_until` on the estimate is authoritative.** `estimate_expiry_days` (60)
  is the **default used to populate it** when an estimate is sent.
- **Per-estimate override falls out for free** — a large commercial quote can carry
  90 days while a small repair carries 30, without a second setting.
- **The customer can see it.** "Valid until 28 Sep 2026" on the PDF and email makes
  the expiry defensible instead of arbitrary — and the UI already has the slot
  (once `expires_at` → `valid_until` is fixed).
- Expiry becomes a date comparison. No re-derivation, no ambiguity.

> **This also removes a trap my §15.5 design walked into.** With expiry computed as
> `sent_at + 60 days`, reopening by flipping status back to `sent` leaves `sent_at`
> months in the past — so **the nightly task re-expires it the same night.** A
> guaranteed loop, and the reopen feature would look broken at random. Anchoring on
> `valid_until` eliminates it: reopening moves the date forward, and the sweeper
> agrees by construction.

#### Reopen

**Mechanically trivial** once anchored on `valid_until`: set a new future date and
flip `expired` → `sent`. The design work is in the guardrails.

1. **Explicit, office-only action.** A "Reopen" button on an expired estimate.
   Never automatic.
2. **Show what changed — this is the real point.** Doug's "the numbers are still
   good" is *sometimes* true, and the reopen screen is exactly where to prove it:
   flag any line whose catalog price has moved since the estimate was written, so
   the office reopens knowingly rather than honouring a stale price by accident. A
   silent revive is the failure mode worth designing against.
3. **New `valid_until`** — defaulting to today + the 60-day setting, editable.
4. **`declined` should be reopenable too.** "People come back" applies at least as
   much to someone who said no as to someone who never answered. Note
   `_ensure_editable` (`routers/estimates.py:198`) currently blocks editing
   `accepted` and `declined` — **`expired` is already editable**, so reopening an
   expired one needs no change, but reopening a declined one means lifting that
   guard for the reopen path. Keep the original `declined_reason` visible in the
   history rather than clearing it.
5. **`accepted` is not reopenable.** That's won work; a change is a change order
   (which exists) or a new estimate.
6. **Win/loss integrity.** A reopened-then-won estimate must count as won, and the
   fact that it was once expired or declined must survive in the trail — the
   reopen must not launder the history. Per §13: `estimate_reopened` with old
   status, old and new `valid_until`, and the actor.

#### Also worth fixing while in here

`expire-stale` filters `status IN ('sent', 'draft')`. Expiring a **draft** is
questionable — it was never sent to anyone, and stale drafts are already
`estimate_draft_archive_days`' job. Recommend narrowing it to `sent`.

**UI (per §14's discipline):** the Reopen action and the price-drift warning are
desktop/office. `valid_until` should also become visible and editable where an
estimate is composed, and printed on the customer-facing PDF/email.

---

## Suggested order

Revised after the §10 audit:

| # | Item | Why first |
| --- | --- | --- |
| 1 | §4 **error handling only** (dialog closes, list refreshes) — not appointment creation | Actively creating duplicate jobs. Audit A8: switching appointments on is a separate change with its own blast radius |
| 2 | **§12 supersede model** — one live closeout per job + shared current-row reader + fix `tech_efficiency` | Now a **prerequisite for §1 and §8**, not a follow-up. Without it the closeout GET 500s on any redone job and the efficiency report double-counts |
| 3 | §1 closeout GET + office/mobile cards (**with A4 signature redaction**) | The original complaint; unblocks billing from real data |
| 4 | **A1 — replace `_job_belongs_to_tech` with `job_belongs_to_user`** | Small fix, hard blocker: without it field billing 404s on ~90% of jobs |
| 5 | **Decide the §12 correction route** (adjustment invoice vs void-and-reissue) | Last open product question; needed before §8 can define what happens to a billed job whose hours change |
| 6 | §9 canonicalize `job_type` + backfill 12 rows + **§14 Gap 1: add the `job_type` picker to `MobileJobNewDialog`** | Cheap, fixes 4 already-broken readers. Gap 1 is a §8 prerequisite: without it a mobile-created install bills hourly and underbills by ~$150 |
| 7 | §11 `techs_on_site` + hours confirmation + **§14 Gap 2 office verification queue** + **Gap 3 tech-side "awaiting verification"** | The safety rails. Ship with or before §8 — §8 without these bills typed-from-memory numbers straight to customers. Gap 3 matters: without it the tech repeats §4's "it didn't work" and re-submits |
| 8 | §8 invoice-from-closeout, **with §15.1's estimate lane first in precedence** + §12's invoice-follows-change rule + **§14 Gaps 4–6** (closeout history, discrepancy alert, supplemental-invoice action) | The field-billing ask — gated on items 2, 4, 5, 6 and 7. The estimate lane is mostly existing code; the matrix is the install *fallback*, not the primary |
| 9 | §6 reconcile `tenant_settings` | Gate behavior is currently a coin flip |
| 10 | §5 job-number uniqueness + backfill (**A3 order: index LAST**) | Stops future collisions; needs your call on renumbering |
| 11 | §3 time-entry display + cost-rate fallback to the configured $65 | Correctness of what the office reads; margin is wrong until this lands |
| 12 | §2 hide `/billing` for `technician` | Small; stops throwing permission errors at techs |
| 13 | §7.3 NULL-safe predicate + fixture | Cheap; closes a live trap before it bites |
| 14 | §13 chain-verify endpoint + periodic check + **§14 Gap 7** (button in `AuditLogViewer`) | Cheap; the tamper-evidence is currently never exercised |
| 15 | **§15.2–15.6 win/loss + expiry** — loss reason, `estimate_expiry_days` setting (default 60) populating **`valid_until`**, revive the dead `expire-stale` sweeper, **Reopen with price-drift warning**, mark-won/lost, `declined`/`rejected` normalization, fix `expires_at`→`valid_until` in the UI | Independent of the closeout chain, so it can run in parallel. $131k of pipeline currently never resolves either way. Depends on §6 (the settings read must key on `tenant_id`, not `LIMIT 1`) |

**§13's audit events are not a separate phase — they ship inside the item that
creates the thing being audited.** No item above is "done" without its events. Same
for the backfill batch records in items 9 and 6.

**§16 (added 2026-07-29, Doug): sales tax must be TRACKED and REPORTABLE.**
Upgraded from the A2 "flag for the accountant" item to a build piece: a sales-tax
report over invoices (tax collected by month/quarter, GDX-generated vs QB-import
provenance, paid vs outstanding), so the accountant question can be answered from
a screen instead of a psql session. The A2 correctness question (construction
contract vs the 7.38% default, `tax_labor=false` inconsistency) stays a human
decision — the report is what makes it answerable.

Verification for §8 specifically: a real closeout → invoice → send on a
throwaway container against real data, in both light and dark mode, plus the
Pixel 8 emulator for the closeout picker (it's a new field-facing control on a
small screen). No "done" until an invoice produced this way has been read
end-to-end.

Not included on purpose: nothing here changes how hours are *captured*.
Attested hours stay the only payable input, and no code added by this plan may
infer hours from elapsed clock time.
