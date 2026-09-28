# Planner "Today" tab — a daily list plus a notes box

**Status: PARTIALLY BUILT** — PR A (migration 098, `services/planner_today.py`, the `/api/planner/today*` routes, the Today tab on desktop and mobile) is built on branch `feat/planner-today`, not yet merged. **PR B (the AI tools) is NOT built.** Audited 2026-09-28 (plan-only `/audit`); findings #1–#5 are folded in below.
**Date: 2026-09-28**

Doug asked for "a spot in the planner that I or AI can edit that is my today's
todo list". He picked option C: pin existing tasks to Today, plus a small
free-text notes box for thoughts that aren't tasks yet.

## What already exists (do not rebuild)

Verified against `origin/main` @ 53746a79 on 2026-09-28.

- **Planner tasks** live in `PlannerTask` / `planner_tasks`
  (`models/tenant_models.py:2125`), served by `routers/planner.py` under
  `/api/planner`, gated by `require_module("jobs")`.
  - "Mine" means `assigned_to == me OR (unassigned AND created_by == me)`
    (`planner.py:154`).
  - A quick-capture task with no due date is due today (`planner.py:228`), and
    the `needs_action` sort puts due and overdue tasks first.
- **Day-granular date convention.** Calendar date D is stored as D 00:00 UTC.
  "Today" is the business-local day (`GDX_BUSINESS_TZ`, default
  America/Chicago), computed by `calendar_today_utc()` and emitted by
  `_date_out()`. The new column uses the same convention and the same helpers.
- **`routers/tasks.py` (`/api/tasks`, `internal_tasks`) is a different table.**
  It is not the planner and is not touched here. The archived call-capture plan
  chose `PlannerTask` for the planner, and so does this plan.
- **Nothing stores per-user free text today.** There is no notes, scratchpad or
  journal table, so the notes box needs a new table.
- **The AI can write almost nothing.** The in-app assistant calls MCP tools
  (`core/mcp_tools/`). `derive_ai_worker_caps` (`core/auth_capabilities.py`)
  limits AI writes to `WRITE_WHITELIST` and AI reads to
  `SUPERUSER_READ_FAN_OUT`. No tool touches planner tasks.
- **Rival plans:** none. A grep of `docs/design/` (including `archive/`) for
  planner, "today" and "my day" found no daily-list plan.

**Prior art** (searched 2026-09-28): Microsoft To Do's
[My Day](https://support.microsoft.com/en-us/todo/my-day-and-suggestions)
resets nightly. Unfinished items go back to their list and appear in the next
day's suggestions; this plan copies that model.
[Open Sunsama](https://github.com/emanuelet/open-sunsama) is a self-hostable
daily planner with an MCP connector. We build in-app instead, because an
external planner would be a second task system with no link to jobs, customers
or calls.

## Decisions

Doug's standing defaults were accepted 2026-09-28.

| # | Decision | Choice |
|---|---|---|
| D1 | Shape | Tasks pinned to Today **plus** one notes box per user per day (option C) |
| D2 | Name | **Today**, a tab inside Planner. It is placed first and opens by default. |
| D3 | Carry-over | **Suggest, don't carry.** Today starts empty each business day. Unfinished pins from the **last 7 days** (7 days, so Friday's pins still show on Monday) and anything due or overdue show as one-tap suggestions. |
| D4 | Audience | Every Planner user. Desktop `PlannerView` and office mobile `MobilePlannerView`. Techs have no Planner (`router/index.js:540`), which is unchanged here. |
| D5 | AI edits | **Apply immediately** (blast radius green). **Every AI write is in the audit trail with `via: "ai"`.** Tasks the AI creates also carry `source="ai"`, and notes the AI saves carry `updated_via="ai"`. A pin or unpin by the AI is marked **only** in the audit trail, because the task row has no "who pinned" column (audit 2026-09-28 #4). |

## Data

### `planner_tasks.today_date`

- New column, `DateTime(timezone=True)`, nullable, D 00:00 UTC convention.
- A task is on Today when `today_date == calendar_today_utc()`. The nightly
  reset is implicit: tomorrow the stored date is no longer today, and nothing
  has to run.
- **Existing rows:** all NULL, meaning "not on Today". No backfill, and no
  money is involved.

### New table `planner_day_notes`

| column | type | notes |
|---|---|---|
| id | String(36) PK | uuid4 |
| company_id | String(36) | same as planner_tasks |
| user_id | String(36), not null | whose day |
| note_date | Date, not null | business-local calendar date |
| body | Text, not null, default '' | capped at 5000 chars by the API |
| updated_by | String(36) | the human, including when the AI wrote it on their behalf |
| updated_via | String(20) | `user` or `ai` |
| created_at, updated_at | DateTime(tz) | |

- Unique on `(user_id, note_date)`.
- No `deleted_at`. Emptying the box is an edit, and every save's resulting
  text goes to `audit_logs`, so the history can be reconstructed (see
  `PUT /today/note`).

### Migration `098_planner_today`

- Inspect-guarded and portable across SQLite and Postgres, the 097 pattern:
  add the column if the table exists and lacks it, and create the table if
  missing. The PG-only `DO $$` of 017 is not reused.
- `downgrade()` drops both. A rollback loses only the pins and the notes.
- **Number collision:** the in-flight vendor-statements branch also claims
  `098`. Whichever merges second renumbers its file.

## API

All routes are under `/api/planner`. The logic sits in a new
`services/planner_today.py`, so the router and the AI tools share one path.
"Mine" is always the caller's own set, as defined above.

- **`GET /today`** returns
  `{date, tasks[], carried[], due[], note: {body, updated_at, updated_via}}`.
  - `tasks`: my tasks pinned today, including ones I finished today, so they
    show checked.
  - `carried`: my not-done tasks whose `today_date` falls in the 7 days before
    today. Older pins drop out of the suggestions but stay in My Tasks (audit
    #3).
  - `due`: my not-done, unpinned tasks due on or before today, excluding any in
    `carried`. Oldest due date first, capped at 15, with a count of the rest
    and a link to My Tasks with the `needs_action` sort.
  - The date is always computed on the server with `calendar_today_utc()`. No
    client or AI supplies "today".
- **`PUT /tasks/{id}/today`** with `{on: bool}` pins or unpins a task and
  writes an audit row (`planner_today_add` / `planner_today_remove`). It returns
  **403** when the task is not in my set. A pin on someone else's task would
  appear on nobody's Today.
- **`POST /tasks`** accepts `today: true` to create a task already pinned. The
  existing `create_task` audit row records `today`. It is refused with 422 when
  `assigned_to` is someone else, because Today is the creator's own list.
- **Reassigning a pinned task** (`PATCH` with a new `assigned_to`) takes it off
  Today and writes a `planner_today_remove` row with `reason: "reassigned"` and
  the from/to assignees. A pin never travels to another person's list (PR A
  audit, round 2: both paths were reproduced leaking a pin to another user).
- **`PUT /today/note`** with `{body, base_updated_at}` upserts today's note.
  - Only today's note is writable.
  - **Optimistic concurrency** (audit #1): if the stored `updated_at` differs
    from `base_updated_at` (null means "I saw no note"), the save is refused
    with **409** and the current `{body, updated_at, updated_via}`. Nothing is
    overwritten silently.
  - Each save writes one `planner_day_note_save` audit row with
    `{note_date, via, before_len, after}`. Before-text of save N is the after of
    save N−1, so the full history rebuilds from `after` alone and each row
    carries one copy of the text, not two (audit #2).
  - A save whose body is unchanged writes nothing and gets no audit row.
  - After a `day_changed` conflict (a page left open past midnight), *Keep
    mine* writes the browser's text as **today's** note on top of whatever
    today already holds. That is deliberate: both texts are on screen when the
    user chooses.
- **`TaskIn.source`** rejects `"ai"` from HTTP callers with 422. Only the
  service sets it, so the `AI` tag cannot be faked from the UI (audit #4).

## AI tools (PR B)

- **Tools:** `planner.today_get` (read), `planner.today_add_task`,
  `planner.today_set` (pin or unpin by task id), and `planner.today_note_write`
  (`mode: append|replace`). Append runs server-side against the stored body,
  so it cannot lose a concurrent edit the way a client read-modify-write can.
- **Freshness:** `AIAssistantPanel` has no event after a tool is applied
  today. PR B fires the existing `gdx:planner-refresh` window event (already
  dispatched by the quick-capture sheet, `AppBottomNav.vue`) after any
  `planner.today_*` tool succeeds. The Today tab already refetches on it, and
  on window focus and visibility (PR A), without moving the note's base
  version under unsaved typing (audit #1).
- **Capabilities:** `("read","planner.today")` and `("write","planner.today")`,
  added to `SUPERUSER_READ_FAN_OUT` and `WRITE_WHITELIST`. All tools are green
  with no approval (D5).
- **Whose day:** `principal.delegated_by_user_id`, else `identity_id`. It must
  resolve to a real `User` row, or the tool refuses.
- **Marking AI writes:** tasks the AI creates carry `source="ai"`, and notes
  carry `updated_via="ai"`. The service's audit rows record `via: "ai"`, in
  addition to the invoke layer's `mcp.tool_invoke` row.

## UI

**Desktop `PlannerView`.** A new first tab, **Today**, open by default. Top to
bottom:

1. **Notes box.**
   - It autosaves on blur and after a 2 s pause, with a "Saving… / Saved" hint
     and "edited by AI at 10:42" when `updated_via=ai`.
   - It refetches when the window regains focus or becomes visible, but only
     while there are no unsaved local changes.
   - **On 409** it shows both texts, "Latest (by AI at 10:42)" and "Yours",
     with *Use latest* and *Keep mine* buttons. *Keep mine* re-saves against
     the new `updated_at`. Nothing is dropped without the user choosing.
2. **Quick add** ("Add a task for today", Enter to add).
3. **Today's tasks.** A checkbox marks done through the existing PATCH, × unpins
   the task, and clicking one opens the existing task dialog. An `AI` tag shows
   on `source=ai` tasks.
4. **Suggestions**, with "Unfinished from earlier" and "Due / overdue"
   groups; + adds an item to Today.

A sun toggle on each card in My Tasks pins or unpins that task from Today.

**Mobile `MobilePlannerView`.** The same Today tab, placed first, in a
single-column layout.

## Packaging

- **PR A:** migration, model, service, router, desktop and mobile UI, and
  tests. Usable on its own.
- **PR B:** the AI tools and the capability ceiling, stacked on A.

## Known gaps (not in scope, for Doug to rule)

1. `PATCH` and `DELETE /api/planner/tasks/{id}` write **no audit row**, and
   neither do link-customer, plans or threads (`planner.py:399–679`). Only
   create writes one. The Today checkbox reuses PATCH, so marking a task done
   leaves no trail, as it does today on My Tasks.
2. The **external** MCP connector (claude.ai OAuth) builds a synthetic UUID5
   identity (`mcp_fastmcp_bridge.py:112`) that maps to no user. The Today
   tools refuse it, so only the in-app assistant can edit Today.
3. **The technician boundary is UI-only.** `/api/planner` is gated by
   `require_module("jobs")` alone (`planner.py:28`) and has no role check, so
   a tech can call it directly. That is existing behaviour and is unchanged
   here (audit #5).
4. The AI ceiling hands write tools to admin and owner delegators only.
   Non-admin office roles get no AI capabilities at all (`caps_for_role`). That
   is existing behaviour and is unchanged here.
