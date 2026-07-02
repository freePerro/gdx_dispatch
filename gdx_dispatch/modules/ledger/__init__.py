"""GL ledger module — GDX's internal double-entry general ledger.

Phase 1 core: chart of accounts, an append-only journal (entries + lines), and
the DB-level integrity that makes the journal the immutable money truth
(balance invariant, immutability triggers, and cross-transaction sealing — all
in migration 012). Nothing posts until the engine (S3) and chokepoint (S4)
land and ``ledger_posting_enabled`` is turned on; S1 only stands up the tables
and their guarantees.

See docs/design/gl-phase1-core-ledger.md (spec) and
docs/design/gl-phase1-implementation-plan.md (slice sequencing). The engine
posts to stable account *roles* (GlAccount.role), never to account numbers, so
the chart of accounts stays operator-editable without breaking posting rules.
"""
