"""SS-34 — Disaster Recovery drill machinery.

This package implements the automated DR drill pipeline:

1. :mod:`backup_snapshot` — pg_dump wrapper + sha256 integrity manifest.
2. :mod:`restore_to_staging` — downloads + verifies + pg_restore into
   an isolated staging DB.
3. :mod:`verification_harness` — canonical read-only sanity checks run
   against the restored staging DB (row-count ranges, RLS policies,
   known critical rows, audit hash-chain intact).
4. :mod:`drill_orchestrator` — sequences snapshot → restore → verify →
   emit events → write audit row. Idempotent on ``drill_run_id``.

A drill is **scheduled**, not ad-hoc: every drill carries a
``drill_run_id`` and a ``scheduled_for`` timestamp so re-calling the
orchestrator with the same id returns the prior report rather than
running the pipeline again.

TODO
----------------

- Router not mounted in ``gdx_dispatch/main.py`` yet.
- ``platform_ss34_additions`` model stub lives on ``SS34Base`` —
  not mounted on the primary platform ``Base``.
- Alembic migration ``TODO_ss34_dr_XXXX.py`` sits on placeholder
  ``down_revision = "TODO"``.
- Event schemas ``gdx.dr.*.v1`` live under ``core/event_schemas/``
  but are not registered in an event-schema index yet (SS-23).

The drill orchestrator NEVER runs against production on its own.
Callers must supply an explicit ``staging_db_url`` that is not
``DATABASE_URL`` — :func:`drill_orchestrator.run_drill` refuses if
the target URL matches production.
"""
from __future__ import annotations

# Submodules are imported lazily by callers to avoid a cascading import
# error during partial build-out (each slice lands as a separate commit).
