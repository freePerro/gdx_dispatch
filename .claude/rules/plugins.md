---
paths:
  - "gdx_dispatch/plugin_api/**"
  - "gdx_dispatch/plugin_host/**"
  - "plugins/**"
  - "gdx_dispatch/routers/*plugin*"
  - "gdx_dispatch/core/plugin_*"
  - "**/*Plugin*.vue"
---

# Plugin sharp edges

Moved here from `CLAUDE.md` on 2026-10-11 so it loads when a matching file is read or written with the Read or Write tool, or read with a plain `cat`/`head` of the file — not on pipes, `wc`, Bash edits (`sed -i`, heredocs, scripts), the Grep tool or a directory-wide search, and not in every session's preamble.

- Plugin `type: list` screens must return a bare JSON array, never
  `{items: [...]}`.
- Plugin manifest handling: warn-and-strip unknown fields, never raise —
  raising during manifest parse silently removes the plugin.
- **`pip install --target` does not replace an existing package directory.**
  A plugin artifact upgrade leaves the OLD code in place while writing the NEW
  dist-info, so `/ready` is green and stale-detection is fooled. Remove the
  package dir and both dist-infos, then restart. The plugin tables also drift:
  `schema_reconcile` auto-ALTERs them at boot.
- The local stack's `refresh.sh` does not rebuild the plugin-host; rebuild it <!-- ~/gdx-local/refresh.sh, outside this repo; link-ok -->
  by hand or you are testing stale plugin code.
