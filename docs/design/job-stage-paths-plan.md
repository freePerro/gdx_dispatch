# Job stage changes go through the paths that own them

**Date:** 2026-10-04
**Status:** MERGED #842 — §4.1, §4.1b, §4.2 and §4.3. Two §7 gaps closed after it on branch `fix/job-stage-guard-gaps` (2026-10-05): a label naming no stage, and the public API's PATCH. Not built: §4.4 backfill, dropped by D2. Revised after `/audit` 2026-10-04 (§8); Doug ruled on every product question on 2026-10-04 (§2).

**Trigger:** the 2026-10-03 lifecycle audit (the local found-not-filed ledger, "job lifecycle
audit") found that every desktop stage change goes through `PATCH /api/jobs/{id}`,
which has no transition guard and doesn't sync the fields that go with a stage.
Its sibling finding, the Re-open dialog never offering Un-complete or Reactivate,
is fixed by PR #841, which this plan's PR A stacks on.

---

## 1. What happens today (verified 2026-10-04, main @ 55605736 + #841)

- **"Complete Job"** (`JobDetailView.vue:2668`) and the **stage strip**
  (`JobDetailView.vue:2284`) both send `PATCH {status}`. The Jobs list edit
  dialog (`JobsView.vue:1217-1218`) sends `status` and `lifecycle_stage` on
  every save, from a six-button bar that includes Complete and Cancelled
  (`JobsView.vue:641`).
- `update_job` (`routers/jobs.py:1186-1192`) writes whatever stage it is given.
  It never sets `completed_at`, `started_at` or `dispatch_status`, sends no
  `job.completed` webhook, skips the tenant's completion requirements, and lets
  a completed or cancelled job move to any stage with no reason.
- **Prod (read-only, 2026-10-04):**
  - Completion requirements on the live tenant (`a1b2c3d4…`): **parts and
    hours ON**, signature and invoice off. (A stale `tenant_settings` row for
    `00000000-…-0001` has signature on; the first draft of this plan misread
    it. /audit finding 2.)
  - Audit log: 41 `job_closeout`, 1 `job_completed`, 204 `job_updated`.
  - 31 jobs have `lifecycle_stage='completed'`, `status='Complete'` (the PATCH
    spelling) and no `completed_at`. 23 of them have exactly one `job_updated`
    audit row whose fields include `lifecycle_stage`; 8 have none (audit
    history starts 2026-06-27). None of the 23 has a time entry; 7 have no
    invoice; several were completed in batches months after creation
    (2026-08-11 08:16 ×2, 2026-08-12 03:09–03:12 ×3) — cleanup, not work.

## 2. Decisions (Doug, 2026-10-04)

| # | Question | Ruling |
|---|---|---|
| D1 | What does desktop "Complete Job" do? | **Open the closeout sheet** — the one the Dispatch board and the phone use. |
| D2 | Backfill the 31 undated jobs? | First **yes, from the audit log**; then, after /audit showed the audit timestamps are mostly cleanup clicks that would move 14 of 23 jobs into later report months, **leave them blank**. Reports keep falling back to the creation date (`reports.py:276`, `:780`). No migration. |
| D3 | How strict is the server? | **Guard finished jobs.** Refuse to move a completed/cancelled job through PATCH, and refuse to complete through PATCH. Cancelling via PATCH keeps working until a real cancel flow exists. |
| D4 | How does the office finish a job with no work to attest? (raised by /audit finding 1) | **Add "Close without work"**: reason required, no hours, no parts, no invoice draft; anyone who may write the job. Real work still goes through the closeout sheet. |

Doug's note on D1: the closeout sheet also needs a way to say "this job needs
another visit" without opening a new job. That is not in this plan. It is the
subject of the multi-day jobs plan (PR #843), where Doug ruled on 2026-10-04
that the closeout sheet asks "Is this job finished?" and "No" saves a daily
log on the same job. Not built here.

## 3. What already exists (do not rebuild)

| Need | Already there |
|---|---|
| Completion with requirements, attested hours, closeout record, invoice autodraft | `POST /api/jobs/{id}/closeout` (`closeout_job` in `routers/jobs.py`). It does **not** send `job.completed`; only `/complete` and `/close-without-work` do (third /audit, finding 3). |
| A desktop UI for it | `MobileJobCloseoutDialog.vue`, already mounted on desktop by `DispatchView.vue:715` |
| Un-complete / reactivate with a required reason and audit row | `/uncomplete`, `/reactivate` and `JobStateOverrideDialog.vue` (fixed by #841) |
| Mark a job as never getting an invoice | `POST /api/jobs/{id}/not-billable` (`invoices.write`). Kept separate from D4: a job closed without work still shows in Ready-for-Billing, so the office can bill a trip charge or mark it not billable as today. |

## 4. Design

### 4.1 Server guard in `update_job` (PR A)

Applies only when the patch resolves to a `lifecycle_stage` **different from the
stored one**. A patch that resends the current stage (the Jobs list edit dialog
does this on every save) and any patch with no stage pass untouched.

| Stored stage | Requested | Result |
|---|---|---|
| `completed` or `cancelled` | anything else | **409** `{"detail": ..., "use": "reopen"}` — use Re-open (`/uncomplete`, `/reactivate`) |
| not `completed` | `completed` | **409** `{"detail": ..., "use": "closeout"}` — use Close out or Close without work |
| open stage | `cancelled` | allowed (only cancel path today) |
| open stage | `in_progress` | allowed; also stamps `started_at` if empty |
| open stage | other open stage | allowed, unchanged |

No audit row on a refusal: nothing changed.

### 4.1b `POST /api/jobs/{id}/close-without-work` (PR A)

- Body `{reason}`; reason required (≥4 characters, same validator as
  `/uncomplete`). Gated on `jobs.write` plus the same object-level check as
  the other job verbs (`job_write_denial`). Per D4 that admits anyone who may
  write the job, an assigned technician included; the job page shows the
  button to office roles only (`patchable`), which is a UI choice, not the
  server's rule.
- Closes any open arrival timer on the job at zero minutes, exactly as
  closeout does for unattested timers, and lists them in the audit row. A
  no-show after "I'm here" would otherwise leave a timer open forever (second
  /audit, finding 1).
- Only from an open stage: a completed or cancelled job gets 409.
- Sets `lifecycle_stage='completed'`, `status='Completed'` (the spelling
  `/complete` and closeout use), `completed_at=now`, `dispatch_status='done'`;
  sends `job.completed`. Writes **no** closeout row, time entry, parts or
  invoice, and does not evaluate the tenant's completion requirements.
- Audit `job_closed_without_work` with the reason and the requirement flags
  that were skipped, so "who closed this with no attested work, and why" is
  answerable.

### 4.2 Job page (PR A)

- **Complete Job** opens `MobileJobCloseoutDialog`; `closed-out` refetches.
- **Close without work**: a secondary header button on open jobs, shown to
  office roles (the existing `patchable` gate), opening a small reason dialog.
- **Stage strip**:
  - "Complete" → the closeout sheet.
  - Any stage on a completed or cancelled job (stored stage, read from
    `lifecycle_stage_raw` as #841 does) → the Re-open dialog.
  - Other moves, including "In Progress", → `PATCH` as today (which now
    stamps `started_at`). Not `/start`: it assigns the clicker as the tech
    and has no stage check (/audit finding 5).

### 4.3 Jobs list edit dialog (PR A)

- The status bar drops **Complete** unless the job is already complete (so a
  re-save still resends the same value). Completion lives on the job page and
  the Dispatch board.
- On a completed or cancelled job the bar is read-only, with a line pointing to
  Re-open on the job page.
- A 409 from the guard is shown in the form's existing error line.

### 4.4 Backfill — dropped (D2)

Kept for the record; not built.


- Candidates: `lifecycle_stage='completed'`, `completed_at IS NULL`,
  `status='Complete'`, not deleted.
- For each, the `job_updated` audit rows for that job whose details mention
  `lifecycle_stage`. **Exactly one** → `completed_at` = that row's
  `created_at`. Zero or several → skipped, and logged by id.
- Matching is done in Python, normalizing ids, so it works on SQLite (dashless
  hex `Uuid`) and Postgres alike.
- Each backfilled job gets an audit row, `job_completed_at_backfilled`, with
  the value and the source audit row id. **Downgrade** reads those rows,
  clears `completed_at` on exactly those jobs, and writes a reverting audit row.
  It never deletes audit rows.
- Expected on prod: 23 backfilled, 8 skipped.

## 5. PRs

- **PR A** (stacked on #841): §4.1–4.3, with tests. Backend tests for every row
  of the §4.1 table and for §4.1b. The allowed moves' stage write is a no-op
  on SQLite (`jobs.py:1257-1271`), so the tests assert the refusals (which
  happen before any write) and `status`/`started_at` on SQLite, and the stored
  stage only where the Postgres arm runs. Vitest for the job page routing.
  The Jobs list edit dialog (§4.3) has **no** spec: the only JobsView spec
  re-implements its save path instead of mounting the view, so it cannot see
  the status bar. §4.3 was proven in a browser instead, and the server's 409
  still refuses the moves it hides (fifth /audit, finding 4).

## 6. Verification

- Full matrix via `run_tests_split.sh`, every FAIL and SKIP named; ruff ratchet.
- Browser, throwaway container, light and dark, desktop and mobile width:
  Complete Job opens the sheet, a closeout completes the job with
  `completed_at` set; stage strip on a finished job opens Re-open; Jobs list
  edit on a finished job is read-only; a forced PATCH gets the 409.

## 7. Out of scope (found, not filed)

- "Needs another visit" on the closeout sheet without a new job → the multi-day jobs plan (PR #843).
- No real cancel flow; cancelled jobs on the tech's Today list.
- `status` spelled "Complete" by PATCH vs "Completed" by `/complete` and closeout; reports that count only one.
- The public API's own stage writes (`api/public_router.py`) bypass `update_job` and this guard.
  **Closed 2026-10-05** (`fix/job-stage-guard-gaps`): its PATCH shares `_stage_change_refusal`. Create is not covered: on Postgres its INSERT omits NOT NULL `dispatch_status`/`company_id` and cannot insert at all — a separate defect.
- The 162 completed jobs with blank status and no `completed_at` (no audit trail to date them), and the 31 left blank by D2.
- `/start` assigns the clicker and has no stage check (/audit finding 5).
- The Dispatch board drawer offers Close out but not Close without work.
- `job.completed` fires for jobs closed without work, with nothing in the
  payload saying so — while a real closeout never sends it at all (prod has 0
  webhook subscriptions today).
- `PATCH /api/jobs/{id}` still rewrites a finished job's `status` when the
  label maps to no stage (e.g. "Scheduled Later"); no frontend sends one.
  Predates this change.
  **Closed 2026-10-05** (`fix/job-stage-guard-gaps`): such a label is now a 422.
- The MCP `jobs.update_status` tool previews and never writes.

## 8. Audit findings (2026-10-04, on the first draft)

1. **Closeout-only completion forces invented hours** (hours > 0 is required on the live tenant; no "no hours" attestation). Accepted → D4.
2. **Signature is not required on the live tenant**; the draft read a stale row. Accepted → §1 corrected.
3. **The backfill dates cleanup clicks and moves report months.** Accepted → D2 reversed, §4.4 dropped.
4. **Backfill audit rows and downgrade under-specified; SQLite cannot prove the allowed stage writes.** Backfill half moot; test half accepted → §5.
5. **`/start` assigns the clicker and has no stage check.** Accepted → strip uses PATCH.

Second `/audit`, on the built diff (2026-10-04):

1. **Close without work left an open arrival timer open forever.** Accepted → timers closed at zero, tested.
2. Complexity: nothing material.
3. Same as 1.
4. **The plan called the verb office-only; the server admits an assigned tech.** The server matches D4; §4.1b and §4.2 now say so, and the route also carries `jobs.write`.
5. **`job.completed` doesn't mark a no-work close.** Recorded in §7; no subscribers on prod.

Third `/audit`, final diff (2026-10-04): no code blocker; `/complete` refactor
verified behaviour-identical. Findings: a non-stage `status` label still
rewrites a finished job (pre-existing, §7); **§3 and a code comment claimed
closeout sends `job.completed` — it does not**, corrected; no other stage
writer found.

Fourth `/audit` (2026-10-04): the round-3 record corrections are accurate and
the only change; flagged that the three new files were still untracked, so a
commit of tracked files alone would ship the guard without its tests and this
plan. They were staged.

Fifth `/audit`, staged state (2026-10-04): staged content equals what rounds
3–4 reviewed. Finding 4: §5 promised a vitest spec for the Jobs list edit
dialog that does not exist. Corrected in §5; §4.3 rests on the browser walk
and the server's 409.

A sixth, single-line review confirmed that correction and asked for this
record of rounds four and five.
