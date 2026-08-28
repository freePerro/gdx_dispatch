# Inbox: Outlook flag mirrors + flagged mail sorts to the top

Status: **RELEASED v1.107.0** (MERGED #510, 2026-08-27; prod + demo 2026-08-28). Prod walk: see §Walk.

## What already exists (do not rebuild)

- The GDX inbox is a **mirror** of the M365 mailbox via Graph delta sync
  (`modules/outlook/tasks.py`), with `isRead` already flowing both ways
  (`PATCH /api/outlook/messages/{id}/read`).
- Stored deltaLinks replay Graph's encoded `$select` verbatim (2026-07 fix).

## Decision

Doug asked (2026-08-27) whether a message **pinned in Outlook** could show
pinned in GDX. It cannot: Outlook's pin has no Graph surface — the v1.0 and
beta `message` resources (fetched 2026-08-27, doc date 2024-08-23) have no
property mentioning it. The **follow-up flag** does (`flag.flagStatus`), so
the flag is the sync-able stand-in. Habit change on the Outlook side: *Flag*
rather than *Pin* (or both).

## Built

| Piece | Where |
|---|---|
| `flag` added to `_DEFAULT_SELECT`; `set_message_flag()` PATCH | `modules/outlook/graph_client.py` |
| `outlook_messages.is_flagged` | `modules/outlook/models.py`, migration `082_outlook_message_flag` |
| Persist `flagStatus == "flagged"` on every sync | `modules/outlook/tasks.py::_persist_messages` |
| Migration 082 also sets `full_resync_required` on every folder — one re-walk so existing mail gets its real flag value | `082_outlook_message_flag.py::_force_resync` |
| `PATCH /api/outlook/messages/{id}/flag` — Microsoft first, mirror on success, audited | `modules/outlook/folders_router.py` |
| Server order: `is_flagged DESC, received_at DESC` (folder list + search) | `modules/outlook/views_router.py` |
| Desktop: flag icon + orange rail, Flag/Unflag in the ⋯ menu, client sort flagged-first | `frontend/src/views/InboxView.vue` |
| Mobile: flag icon on the card, Flag/Unflag button on the detail | `frontend/src/views/MobileInboxView.vue` |

## Trap recorded (and the wrong fix the audit caught)

Adding a field to `_DEFAULT_SELECT` silently does nothing for any folder that
already holds a deltaLink — Graph bakes the select **inside the opaque
`$deltatoken`** and keeps replaying the old shape until the folder is
re-walked. Without a resync, `is_flagged` would have stayed FALSE on prod
indefinitely while every test passed.

First attempt: a generic guard that parsed `$select=` out of the stored URL
and re-walked when a field was missing. `/audit` read prod (63 folder rows):
**no deltaLink carries `$select=` in its query string**, so the guard would
have judged every folder stale on every sync — a full mailbox walk per
webhook, forever, and deletions (only visible via a real delta replay) would
have stopped mirroring. Tests passed because the fixtures were written to
match the belief, not a captured link. Replaced by the one-shot
`full_resync_required` in migration 082 — the same path the 410 handler uses.
**Rule: when you add a field to `_DEFAULT_SELECT`, ship a migration that sets
`full_resync_required = TRUE`.**

## Not built / open

- `complete` flags read as unflagged (deliberate: the top is for what still
  needs attention).
- Flag due dates / reminders are not mirrored.
- Prod walk after release; the first sync after deploy re-walks each folder
  once (bounded by Graph paging; upsert is idempotent).
- The wider "what else can the inbox do" research is a separate doc:
  `docs/design/m365-mail-platform-options.md` (in progress).

## Walk (prod, 2026-08-28, v1.107.0)

- Deploy: `update.sh` ran migration `081 → 082` on boot; alembic head `082_outlook_message_flag`;
  all of `gdx-app-1`, celery ×3, plugin-host on `1.107.0`; `/health` ok; edge 200. Demo on 1.107.0.
- After 082, all 63 folder sync rows were `full_resync_required` with tokens cleared. One
  `sync_outlook_mailbox` run re-walked them in **39 s** (65 folders, 1,132 upserts, 0 removed):
  62/63 re-tokened — the 63rd is Junk Email, which is in `SKIP_SYNC_WELL_KNOWN` and never syncs.
- **9 of 3,136 messages came back flagged** — real flags set in Outlook, across Inbox, Estimates
  and five custom folders. Headed browser walk as the auditor account: the three visible flagged
  messages (Aug 21, Mar 11, Dec 23) sit above Aug 27 mail, flag icon + orange rail, ⋯ menu shows
  **Flag** on an unflagged row; light and dark both clean; footer reads v1.107.0.
- GDX→Outlook write: **owner-confirmed 2026-08-28** ("flags work good"). (The auditor has
  no Outlook account, so `/flag` 404s for it by design — only the owner session can prove it, and it
  did.)
- Side finding: the re-walk re-collected vendor-bill candidates and one allow-listed message fails
  `upload_midwest_invoice` with a `varchar(60)` truncation every time — pre-existing, isolated per
  message, filed separately.
