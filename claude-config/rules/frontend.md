---
paths:
  - "gdx_dispatch/frontend/**"
---

# Frontend sharp edges

Moved here from `CLAUDE.md` on 2026-10-11 so it loads when a matching file is read or written with the Read or Write tool, or read with a plain `cat`/`head` of the file — not on pipes, `wc`, Bash edits (`sed -i`, heredocs, scripts), the Grep tool or a directory-wide search, and not in every session's preamble.

- jsdom applies **no media queries** — only a real browser proves layout.
- `useDestructiveConfirm` really confirms in the app — issue #215 was fixed
  2026-08-05 (`useConfirm()` now resolves during `setup()`). The fallback
  still auto-accepts when no ConfirmationService is registered, which is
  every vitest environment: unit tests never exercise the dialog, so a
  destructive flow is only proven in a browser.
- The frontend lockfile can only be regenerated on **npm 11**: 10.8.2 (what
  CI's `setup-node` 20 and `node:20-slim` ship) and 9.2.0 both crash in
  arborist with `Cannot read properties of null (reading 'edgesOut')`. Prove
  `npm ci` in `node:20-slim` before pushing a lockfile.
