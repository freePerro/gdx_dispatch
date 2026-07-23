# Mobile: customer add/search pain + job create "doesn't save" — fix plan

_Drafted 2026-07-22. Doug's report: "(1) in mobile it is a pain to add a new
customer / search customers, (2) creating a new job does not work correctly
or save."_

## Diagnosis (verified against source at HEAD)

### Complaint 2 — "creating a new job does not work correctly or save"

The POST itself succeeds. Three real defects make it *look* broken and lose
data:

**J1. The created job is invisible to its creator.**
`MobileJobNewDialog` intentionally creates jobs with no tech and no
schedule (dispatch assigns later). But `GET /api/mobile/jobs`
(`routers/mobile.py:1320` `mobile_all_my_jobs`) returns only jobs where
`j.assigned_to = tech` or a `job_assignments` row matches. So after
"Create job" the reload can never show the new job — the code comment in
`MobileJobsView.onJobCreated` ("shows up in All") is false. Worse, for a
user with no `technicians` row (e.g. an admin/owner account), the endpoint
early-returns `{jobs: []}` — the Jobs tab is *always empty*. Net effect:
tech taps Create, toast says success, list shows nothing → "it didn't save."
The job actually sits in the "Ready to Schedule" holding area, visible only
on desktop dispatch.

**J2. Description is silently dropped.**
The dialog sends `description`, but `JobCreate` (`routers/jobs.py:56`) has
no `description` field and `Job(...)` in `create_job` never sets it —
pydantic silently discards the extra key. Whatever the tech typed is lost.
(The `jobs.description` column exists — `models/tenant_models.py:263` —
and `JobUpdate` writes it; only create drops it.)

**J3. Unscheduled jobs are stamped status="Scheduled".**
`JobCreate.status` defaults to `"Scheduled"`, so
`payload.status or derived_status` (`routers/jobs.py:775`) never uses the
carefully derived `"Service Call"` for date-less jobs. The
`derived_status` branch is dead code for any caller that omits `status`
(the mobile dialog, MobileDispatchView). Result: a phantom
status="Scheduled" job with no date, while `lifecycle_stage` correctly
says `service_call` — the two columns disagree.

### Complaint 1 — "pain to add a new customer / search customers"

**C1. Customers is unreachable on mobile.**
The bottom nav (`AppBottomNav.vue`) has no Customers tab for either role
row (tech: Today/Jobs/Clock/Photos/More; office:
Jobs/Clock/Planner/Dispatch/More). *(Corrected per audit: it is not even
in the More drawer — `'customers'` sits in `reservedKeys`, which filters
it OUT of the drawer, evidently written anticipating a tab that never got
added. From the bottom nav, Customers is unreachable, period.)*

**C2. Phone search is broken in the job dialog.**
`MobileJobNewDialog` searches via `GET /api/customers/search`
(`routers/customers.py:375`), which does a plain `LIKE` on the raw phone
column. Stored numbers are formatted ("(612) 555-1234"), so typing digits
from caller ID ("6125551234") matches nothing. The digit-stripping fix was
added to `GET /api/customers` (list) on 2026-05-XX (`customers.py:254-265`)
but never ported to `/search`. `/search` also fails to exclude
"(deleted)"-named rows, which the list endpoint filters.

**C3. Just-created customers vanish for up to 30 s.**
`GET /api/customers` responses are Redis-cached 30 s per
`(q, page, per_page)` (`customers.py:232,312`) and no customer mutation
invalidates the cache. Create a customer, return to the list (or repeat a
recent search) → the new customer is missing → looks like the create
failed, gets retried, makes dupes.

**C4. Dead-end flows.**
- In the job dialog, a zero-result search offers nothing — the tech must
  notice the small "Create new" toggle.
- `MobileCustomerDetailView` has no "New job" action, so
  create-customer→start-job requires re-finding the customer through the
  Jobs tab dialog (where phone search is broken, C2).

## Fixes

### Backend

1. **`JobCreate` contract** (`routers/jobs.py`)
   - Add `description: str | None = Field(default=None, max_length=20000)`;
     write it in `create_job` (`Job(description=payload.description or None)`).
   - Change `status: str` default `"Scheduled"` → `status: str | None =
     Field(default=None, max_length=50)` so `payload.status or
     derived_status` actually derives. *(Corrected per audit: the desktop
     CREATE payload does NOT send status — only the edit branch does — so
     this changes desktop create too: date-less desktop jobs correctly
     become "Service Call" instead of a phantom "Scheduled". Audit
     verified the read side: nothing filters on status="Scheduled";
     display derives from lifecycle_stage. Intended behavior change.)*
     `MobileDispatchView` always sends `scheduled_at` so it derives
     "Scheduled" — same value as today.
   - Return `description` in the create-response dict (harmless, aids tests).

2. **`jobs.created_by` column** — alembic migration `035_job_created_by`:
   `ADD COLUMN created_by VARCHAR(36) NULL` + ORM field on `Job`. Stamp
   `created_by=_user_id(current_user)` in `create_job`. (No backfill —
   NULL means "pre-feature row"; nothing reads it as required.)

3. **Creator visibility — read-only and self-limiting** *(revised per
   audit 2026-07-22)*. `job_belongs_to_user` is the WRITE gate for ~18
   mobile endpoints (start/complete/clock-in/status/signature/…) plus web
   notes/photos — the creator must NOT get a permanent pass-through there
   (clock data is payroll evidence; creator-write on a job reassigned to
   another tech corrupts records). Instead:
   - Visibility rule: a job counts as "mine" for the creator only **while
     it is unassigned** (`created_by = me AND assigned_to IS NULL AND no
     live job_assignments row`). Once dispatch assigns it (to anyone),
     normal assignment rules take over. This also stops the Jobs tab from
     becoming a permanent creation log for office users.
   - `mobile_all_my_jobs` (`routers/mobile.py:1320`): add the
     creator-while-unassigned OR-clause; build the WHERE conditionally
     when the caller has no technician row (no sentinel ids) so creators
     without a technician record still see their pending jobs.
   - Detail: do **not** touch `job_belongs_to_user`. Add a read-only
     allowance in `_assert_job_access`'s *read* path only — a
     `_creator_can_read` check used by `GET /api/mobile/job/{id}` (and any
     GET the detail view needs), same creator-while-unassigned rule.
     Write endpoints keep the existing gate unchanged; tests must assert
     the creator of an unassigned job can GET detail but gets 404 on
     POST /start, /complete, /clock-in.
   - `MobileJobsView` empty-state copy: "No jobs assigned" → mention that
     created jobs appear here until dispatch assigns them.

4. **`/api/customers/search` parity** (`routers/customers.py:375`): port
   from `list_customers` — (a) digit-stripped phone clause when the query
   has ≥7 digits (chained `replace()`, PG+SQLite portable), (b) exclude
   `deleted_at` rows *and* "(deleted)"-named rows.

5. **Customer cache staleness** *(simplified per audit)* — two parts:
   - `list_customers`: bypass the cache whenever `q` is non-empty
     (searches are cheap LIKEs over a few-hundred-row single-tenant
     table; always-fresh search directly kills the mid-typing staleness).
     Keep the 30 s cache for the `q=''` default list only.
   - Add `invalidate_prefix(tenant_id, prefix)` to `core/cache.py`
     (async `scan_iter` over the anchored pattern
     `cache:{tenant_id}:customers:q=:*`, tiny key family) + a sync
     wrapper mirroring `invalidate_sync` — `merge_customers` is a sync
     handler and needs it. Call after create/update/delete/merge commits.

### Frontend

6. **Bottom nav: Customers tab** (`AppBottomNav.vue`) — insert
   `{ key: 'customers', label: 'Customers', icon: 'pi pi-users', to:
   '/mobile/customers' }` into both role rows (before More), and change
   `.bottom-nav` CSS from `repeat(5, minmax(0,1fr))` to
   `grid-auto-flow: column; grid-auto-columns: minmax(0,1fr)` so the grid
   fits any tab count. Verify 6 tabs fit at 360 px on the emulator.

7. **Zero-result → create-new affordance** (`MobileJobNewDialog.vue`) —
   when a search of ≥2 chars settles (not loading) with 0 options, render
   a tappable row: `+ Add "<query>" as a new customer` → sets
   `newCustomer=true`, prefills `newCust.name` (or `newCust.phone` when
   the query is digits-only).

8. **After-create truth** (`MobileJobsView.vue`) — with J1 fixed the
   reload genuinely shows the job (dispatch_status "unassigned" appears
   under Active and All). Update the dialog's success toast to say where
   it went: "Job created — in Ready to Schedule until dispatch assigns it."

9. **"New job" from customer detail** (`MobileCustomerDetailView.vue`) —
   add a header action that opens `MobileJobNewDialog` with a new optional
   `customer` prop pre-selecting that customer (skips search entirely).
   Dialog change: accept `customer` prop and apply it **inside the open
   watcher, between `_resetForm()` and `snapshot()`** — seeded earlier it
   gets wiped; seeded later (separate prop watcher) the dialog is born
   dirty (X/Esc disabled, phantom discard prompt). Vitest must assert
   `isDirty === false` immediately after opening with the prop
   *(audit-predicted shipped bug)*.

### Tests

- Backend (pytest, docker-app image harness):
  - create job with description → row persists description; response echoes.
  - create job without scheduled_at → status "Service Call",
    lifecycle "service_call"; with scheduled_at → "Scheduled". Explicit
    status still honored.
  - created_by stamped; `/api/mobile/jobs` returns creator's unassigned job
    (with and without a technicians row); other techs do NOT see it
    (creator-only, not assigned).
  - `job_belongs_to_user` true for creator; unrelated tech still 404s.
  - `/api/customers/search`: digits query matches formatted phone; short
    (<7 digit) queries unchanged; "(deleted)" rows excluded.
  - cache: `invalidate_prefix` unit test (fake/None redis degrade path).
- Frontend (vitest): MobileJobNewDialog — zero-result affordance flips
  toggle and prefills; customer-prop preseed; existing specs still green.
- Static contract tests (`test_mobile_job_create_contract.py`,
  `test_customer_multi_location_contract.py`, and
  `test_jobs_create_payload_contract.py` — the one purpose-built to watch
  `create_job` payload reads, missed in draft 1) — re-run; they pin
  router deps/regex spans that these edits must not break.
- New security tests *(audit finding 1)*: creator of an unassigned job →
  GET detail 200, POST /start //complete //clock-in 404; after the job is
  assigned to another tech → creator loses list row and detail access.

### Verification (after implementation)

1. Full backend + frontend suites in the docker-app image.
2. Throwaway container + headed Playwright (verifyplaywright): phone
   viewport; real flows — add customer (appears immediately), search by
   formatted-phone digits, create job w/ description from Jobs tab and
   from customer detail, confirm it renders in list + detail shows
   description, light + dark mode.
3. Android emulator (androidTesting): same flows in real mobile Chrome —
   6-tab nav fit, soft-keyboard behavior over the dialog, touch targets.

### Out of scope (deliberate)

- Address search (column is encrypted — can't LIKE server-side).
- Pagination beyond per_page=200 on the mobile customer list.
- Any change to desktop JobsView create flow.
- Backfilling created_by for historical jobs.

### Risks

- `jobs.created_by` migration must reach prod via alembic before the new
  code path writes it (deploy runs migrations first — standard).
- `/api/mobile/jobs` now shows creator-jobs to office users who had an
  always-empty Jobs tab; that's the intent, but check no downstream
  assumption on `tech_id` being non-null in that response.
- Cache `scan_iter` on a shared Redis: keyspace is tenant-prefixed and
  small; match pattern is anchored `cache:{tenant}:customers:*`.
- Static contract tests regex-scan `routers/jobs.py` decorator/model text;
  edits stay inside the model body so spans should hold — verify.
