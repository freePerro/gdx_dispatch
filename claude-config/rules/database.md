---
paths:
  - "gdx_dispatch/migrations/**"
  - "gdx_dispatch/models/**"
  - "gdx_dispatch/services/**"
  - "gdx_dispatch/routers/**"
  - "gdx_dispatch/core/**"
  - "gdx_dispatch/modules/**"
  - "gdx_dispatch/tasks/**"
  - "gdx_dispatch/tests/**"
---

# Database sharp edges

Moved here from `CLAUDE.md` on 2026-10-11 so it loads when a matching file is read or written with the Read or Write tool, or read with a plain `cat`/`head` of the file — not on pipes, `wc`, Bash edits (`sed -i`, heredocs, scripts), the Grep tool or a directory-wide search, and not in every session's preamble.

- **SQLite stores a `Uuid` column as 32 dashless hex.** Raw SQL comparing
  `id = :dashed_uuid` matches on Postgres and **never** on SQLite — and a
  gate that cannot match looks exactly like a legitimate refusal.
- **`create_all` tables diverge from the ORM**, so a schema sweep has to
  search the *database*, not the models. For the same reason test fixtures
  must build their schema from the ORM: hand-written DDL once hid a feature
  that could not be inserted at all.
