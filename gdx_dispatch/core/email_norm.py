"""Single-source email normalization for the auth-identity surface.

Every write site that persists an email — tenant `users.email`, control-plane
`identities.email`, SCIM imports, federation linking, invitations — MUST
normalize through this helper before persistence. Reads continue to compare
case-insensitively (`func.lower(...) == normalize_email(...)`) so the
normalization holds across the round-trip.

Why this exists (audit Finding 4, Sprint Auth & Identity Hardening):
    `Doug@…` and `doug@…` were both written verbatim into prod identities
    rows for the same human. JWTs minted under one row and cookies
    recovered against the other returned "no profile" 500s. The data
    structure permits two identity rows for the same lowercased email
    because there's no UNIQUE INDEX on `lower(email)` and signup wrote
    literal-case email. This helper is the single chokepoint that
    eliminates the asymmetry.

Behavior:
    - Strips ASCII whitespace from both ends.
    - Lowercases the entire address.
    - Returns "" if the input is None / empty after stripping (so call
      sites can branch on truthiness without a NoneType crash).

Out of scope (handled elsewhere):
    - Validation of address shape (`@`, length, RFC) — Pydantic / signup
      input validation owns that.
    - Idempotency at the DB layer — that's the unique-index migration.
"""
from __future__ import annotations


def normalize_email(value: str | None) -> str:
    """Return a canonical lower-cased, trimmed email string.

    Always safe to call with None or empty input. Idempotent — running it
    twice produces the same result.
    """
    if not value:
        return ""
    return value.strip().lower()
