# Release notes — the in-app "What's new" history

Status: MERGED #1090 — the What's new page, its banner, and the release gate

Every file here named `vX.Y.Z.md` is one release's notes. They ship inside
the image and are what users read on the **What's new** page
(`/release-notes`), reached from the version under the sidebar, the user
menu, and the mobile **More** drawer. Old files are never deleted: they are
how someone finds out which release changed the thing that now works
differently.

## Who reads these

Office staff and techs, not developers. Write the way you would explain the
change to them out loud: what they will see, where, and what to do about it.
PR numbers and code names belong in the change list, which is generated (see
below), not in the written notes.

## Format

```
---
version: 1.146.0
date: 2026-10-12
title: Short name for the release
warning: Optional. One sentence about something to do before or after updating.
---
## Highlights
- One plain-language line per change people will notice.

## Changes you may notice
- Smaller visible changes.

## Under the hood
- Things an admin needs to know about (migrations, backups, settings).
```

- Frontmatter keys: `version`, `date` (YYYY-MM-DD), `title`, `warning`.
  Any other key fails the parse. `version` must match the file name.
- Body: `## ` section headings, `### ` sub-headings, `- ` bullets (an
  indented line continues the bullet above it), and plain paragraphs.
  Markdown links keep only their text. Nothing is rendered as HTML.
- `gdx_dispatch/services/release_notes.py` is the parser.
  `gdx_dispatch/tests/test_release_notes.py` parses every file here, so a file
  that breaks the format fails CI rather than the page.

The 214 files for v1.0.0–v1.145.0 were backfilled on 2026-10-12 from the
GitHub Release bodies. They have no title or highlights because none were
ever written, and they stay that way.

## The change list is generated, not written

Each release's full list of merged changes ("Show all changes" on the page)
is computed from git by the Release workflow
(`python -m gdx_dispatch.tools.release_notes changes --write`) into
`changes.json` here. That file is git-ignored and exists only inside a
release image, so a local or dev build shows the written notes with no
change lists.

## Cutting a release

1. Draft the file, which also prints the PRs merged since the last release:

   ```
   python -m gdx_dispatch.tools.release_notes draft X.Y.Z
   ```

   If the file already exists (it was written ahead of the release), `draft`
   leaves it alone and also prints what merged after the file was last
   edited. Fold in anything there that users will notice, and re-confirm
   the `warning:` line for any new migration. `check` refuses a file with
   changes merged after it, so this step is not optional.

2. Write the title and highlights, then check the file:

   ```
   python -m gdx_dispatch.tools.release_notes check X.Y.Z
   ```

   On the notes branch this only warns that the file is not on `origin/main`
   yet. Freshness is judged against `origin/main`, like `draft`.

3. Merge the notes file to `main` through a PR, **before** tagging.
4. `git fetch` and run `check X.Y.Z` again. If anything merged after the
   notes, it fails and lists it: go back to step 1, fold those in, and merge
   the edited file.
5. Tag `vX.Y.Z` on that main commit and push the tag.

The Release workflow runs `check` before it builds anything, so a tag with no
notes file fails in seconds and publishes nothing. So does a tag where
anything other than docs (commits that change only `.md` files) merged after
the notes file was last edited: `check` lists those changes. `check` judges a
file at its own tag once that tag exists. It only warns where git cannot tell
(a shallow clone, or a file not committed yet), and fails on any other git
error, such as a `--ref` that does not resolve.

What `check` proves is that the file was edited after the last code change,
not that the edit covers it: any commit to the file clears the list. Read
what `draft` printed and fold it in. Editing a blank line only silences
`check`. To recover, delete the tag
(`git push --delete origin vX.Y.Z` and `git tag -d vX.Y.Z`), merge the notes,
and tag again. The GitHub Release body is this file's text
(`release_notes body X.Y.Z`), with the warning shown first.
