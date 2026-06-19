# ADR-010: QuickBooks Sync Conflict Resolution Rules

Date: 2026-03-25
Status: Accepted

## Context

GDX and QuickBooks (QB) can both mutate overlapping entities during sync windows.
Conflicts are expected when GDX writes race with direct edits in QB.

Four main conflict scenarios were identified:
1. Invoice or job line items are edited in GDX while the same invoice is edited in QB.
2. Payments are recorded or voided directly in QB while GDX has stale payment state.
3. Customer contact information is changed in both systems between sync cycles.
4. Sync reliability failures occur during conflict handling (QB API rate limits or token refresh failures).

## Decision

Conflict resolution is deterministic and rule-based:

1. RULE 1 — GDX is source of truth for job/invoice LINE ITEMS
   (description, quantities, rates).
2. RULE 2 — QB is source of truth for PAYMENT STATUS
   (paid, partial, void).
3. RULE 3 — MOST RECENT TIMESTAMP wins for customer contact info
   (name, address, phone, email).
4. RULE 4 — QB rate limit is 5 req/sec. Apply exponential backoff with jitter:
   `delay_ms = 100 * 2^attempt + random(0,100)`.
   Retry up to 8 attempts, then fail the sync job with `QBRateLimitError`.
5. RULE 5 — If QB token refresh fails during sync:
   mark sync as `paused`, record `pause_reason` in `QBTokenStore`,
   notify tenant admin via Celery email task, and retry only on next
   scheduled run (no immediate retry, to prevent thundering herd).

Out-of-order event handling:
- GDX entity ID is the idempotency key.
- Idempotency keys must always derive from GDX IDs, never random UUIDs.

Conflict audit trail:
- Every conflict resolution must emit an audit event containing
  `{rule_applied, gdx_value, qb_value, winner}`.

## Consequences

- Line items remain consistent with operational edits in GDX.
- Payment state reflects accounting truth from QB.
- Contact-field conflicts are resolved predictably without manual triage.
- Rate-limit and auth failures fail safely with explicit pause/error semantics.
- QB auth-expired errors must surface to dealer UI within 15 minutes.
