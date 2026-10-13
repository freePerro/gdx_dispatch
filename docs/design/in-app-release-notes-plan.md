# In-app release notes — what's new, and every version before it

Status: MERGED #1090 — all five items built; the shape changed in places, see "As built" below
Date: 2026-10-12

## Ask (Doug, 2026-10-12)

Users need one place in the app that says what changed in the release they
are on, and lets anyone look back at older releases — "hey, this is why this
is going on now". The five items agreed:

1. Plain-language notes kept in the repo, shipped inside the image.
2. A "What's new" page, newest first, with every past release browsable.
3. The sidebar version label links to it.
4. A once-per-user "Updated to vX — see what's new" banner after an upgrade.
5. Admins see "update available" from the existing update check.

## What already exists (do not rebuild)

- `GET /api/admin/update-check` (`routers/admin_ops.py`) — compares
  `APP_VERSION` with GitHub's latest release, returns `notes_url`. Gated
  `settings.read`. **No frontend caller today.**
- `GET /pwa/version` (`core/pwa.py`) — `{version, build_time}`; the sidebar
  build stamp (`components/AppSidebar.vue`) already fetches it. Not a link.
- Per-user "seen at version" storage: `user_tour_progress`
  (`/api/me/tours/{tour_id}/complete`, `routers/tours.py`). Reused for the
  banner — tour id `release-notes-v1-146-0` — so **no migration**.
- 214 GitHub Releases, v1.0.0–v1.145.0, each an auto-generated PR-title list
  (`release.yml` runs `gh release create --generate-notes`). Already public.
- `markdown-it` + `dompurify` are frontend deps — **not used here** (see below).

Prior art searched (2026-10-12): Headway, Beamer, Noticeable, Patchlog are
hosted SaaS widgets; Changerawr, Feedbase, ShipShipShip are self-hosted but
are separate services with their own DB. This app is single-tenant,
self-hosted, and an install may have no egress; the notes are a few hundred
KB of text that already live in the repo. A separate service is more to run
than the feature is worth, so it is built in.

## Design

### Notes files — `gdx_dispatch/release_notes/vX.Y.Z.md` <!-- pattern, not a file; link-ok -->

One file per release, inside the package so `COPY gdx_dispatch/` ships it.

```
---
version: 1.146.0
date: 2026-10-13
title: Time off balances, tax by job type, numbered jobs
---
## Highlights
- Plain-language line a tech or office person understands.

## All changes
- fix(tax): change order totals follow the job lane … (#1073)
```

Only `##` headings and `- ` bullets are allowed in the body. The server parses
them into `{title, items: [str]}` sections and the page renders **plain text**
— no `v-html`, no markdown renderer, no XSS surface. A file that breaks the
format fails a test, not a page.

Backfill: the 214 GitHub bodies become `## All changes` lists (author/URL
noise stripped, `(#N)` kept), dated by `published_at`, with no Highlights —
they never had plain-language notes and inventing them now would be fiction.

### Backend — `routers/release_notes.py` + `services/release_notes.py`

- `GET /api/release-notes` → `{current_version, releases: [...]}`, newest
  first by semver. Any signed-in user (`get_current_user`). Loaded once and
  cached; the files are immutable inside an image.
- `GET /api/release-notes/{version}` → one release, 404 if not bundled.
- Read-only: no audit row, no new mutation route, no authz baseline change.

### Frontend

- `views/ReleaseNotesView.vue` at `/release-notes` and
  `/release-notes/:version` (deep link opens and scrolls to it). Search box
  filters across every release's text, client-side (~200 KB once). The
  installed version is badged. Works at phone width.
- Admins (`settings.read`): the page calls `update-check` once and shows
  "vX is available" with the GitHub notes link. The sidebar stamp shows a
  small "update" dot for them, update-check cached per session.
- `AppSidebar.vue`: the build stamp becomes a router-link to the page.
- `components/WhatsNewBanner.vue` in `AppLayout.vue`: shows when the
  installed version has a bundled notes file and the user has no completed
  `release-notes-v…` tour row. "See what's new" and "Dismiss" both mark it
  complete. Never shown on `dev` builds.
- Ways in: sidebar stamp, Settings link card, mobile "More" drawer.

### Release process

- `tools/release_notes_draft.py vX.Y.Z` writes the file from the merged PR <!-- rejected name, never built; link-ok -->
  titles since the last `v*` tag, with an empty Highlights to fill in.
- `release.yml` fails fast, before building, if
  `gdx_dispatch/release_notes/<tag>.md` is missing or does not parse. The
  GitHub Release body becomes the Highlights prepended to the generated PR
  list (`--generate-notes --notes …`, gh docs: "Additional release notes can
  be prepended to automatically generated notes by using the --notes flag").
- `/GDXrelease` skill gets the notes step (lives outside this repo).

## Out of scope

- Notes for versions newer than the installed one are not in the image; the
  admin notice links to GitHub for those.
- No per-release role targeting, no images.

## As built (2026-10-12) — where it differs from the design above

The design above is kept as written. The audit
(`/audit`, 2026-10-12) and the build changed these parts:

- **Changes are generated, not stored.** The backfill does not write
  `## All changes` lists into the files. Each release's merged-change list is
  computed from git at release-build time
  (`tools/release_notes.py changes --write` → `release_notes/changes.json`, <!-- git-ignored, release images only; link-ok -->
  git-ignored, only inside a release image) and shown behind "Show all
  changes (N)". The files hold only what a person wrote. The backfilled
  v1.0.0–v1.145.0 files carry the old GitHub bodies with an empty title.
- **Body format** is wider than `##` + `- `: `###` sub-headings, plain
  paragraphs and indented bullet continuations are parsed too (into
  `heading` / `paragraph` / `list` blocks), still rendered as plain text.
  Frontmatter may carry a `warning:` shown first on the page and in the
  GitHub body.
- **One tool, four subcommands** instead of `release_notes_draft.py`: <!-- link-ok -->
  `tools/release_notes.py draft | check | changes --write | body [--title]`,
  stdlib-only so the workflow runs it before installing anything.
- **The GitHub Release body is the notes file**
  (`release_notes body <tag>`), not Highlights prepended to
  `--generate-notes`. `release.yml` fetches tags (`fetch-depth: 0`) for the
  change lists and passes `GDX_IMAGE_VERSION` as a build arg.
- **Installed version:** `core/app_version.py` reads `APP_VERSION` when it
  is a version, else the image's `GDX_IMAGE_VERSION`, so an install pinned to
  `latest` still knows what it runs. `/pwa/version` and `update-check` use it.
  `plugin_api/discovery.py` and one `app.py` site still read raw
  `APP_VERSION`.
- **"Seen" rule:** any completed/skipped `release-notes-v…` tour row at or
  above the installed version counts, so a rollback never re-announces notes
  already read. Opening the page also marks the installed release seen. If
  `/api/me/tours` is unavailable, a localStorage copy stands in.
- **Ways in:** sidebar stamp ("vX · What's new", with an update dot for
  `settings.read`), a **What's new** item in the user menu (reaches every
  role, unlike a Settings card, which was dropped), and the mobile More
  drawer. The admin notice says to run `docker/update.sh`.
- The banner is not suppressed on `dev` builds; a dev build has no matching
  notes file, so it never shows there.
