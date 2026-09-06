"""Fold the copied bug reports that already had a support ticket into it.

Revision ID: 090_dedup_copied_bug_reports
Revises: 089_fold_superadmin_into_owner
Create Date: 2026-09-06

Migration 087 copied every `bug_reports` row into `support_tickets` unless a
ticket with the SAME ID existed. The mobile bug button had, for its last
five reports, written both tables in one request — under two different ids
— so on production the copy produced five duplicate rows on the Feedback
page (walked 2026-09-06, v1.117.0). The two oldest reports (July 6) existed
only in the retired table and were copied once, correctly.

A "copy" is a ticket whose body ENDS with the trailer 087 appended (087
wrote it last, so the exact suffix is the classifier — a ticket that merely
quotes the sentence is not a copy). A "pair" is an open original (no
trailer) and an untouched copy (never closed, no resolution, not already
folded) with the same tenant, category, subject, reporter and calendar day,
whose bodies start with the same first line (the description the form sent
to both tables). Copies and originals are matched one-to-one in creation
order, so two pairs sharing a key each keep their own original; a copy with
nothing left to pair with is counted in the log. For each pair this appends the copy's
body — minus the 087 trailer line, so a folded original can never read as
a copy on a rerun — to the original under a divider (the copy carries the
full browser string and page URL the button's own ticket did not) and then
deletes the copy. Every original stays; a copy with no open original, or one
someone already closed, stays. The ids folded are logged at INFO so the
entrypoint log shows exactly what this did (or that it did nothing).

Trail: this repo's audit invariant asks who/what/when. The "when" is
alembic_version and the entrypoint log; the "what" is the logged ids; the
"who" is the release. No audit_logs row is written: the app's audit helper
commits or rolls back on its way in, which cannot be allowed inside a
migration's transaction, and a raw INSERT would break the hash chain.

Timezone: DATE(created_at) is evaluated in the session time zone (UTC on the
shipped stack). The five prod pairs are seconds apart; a pair that straddled
midnight would be missed, not damaged.

Downgrade: a no-op by design. The folded rows' text is on the originals.
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import inspect, text

revision = "090_dedup_copied_bug_reports"
down_revision = "089_fold_superadmin_into_owner"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# What 087 appended to every copy (see its _COPY), and what this appends.
_TRAILER = "\n(copied from the retired bug_reports table by migration 087; same id)"
_DIVIDER = "\n\n--- also recorded by the mobile form (folded in by migration 090) ---\n"

_ROWS = text(
    """
SELECT id, tenant_id, subject, COALESCE(opened_by_user_id, ''), DATE(created_at), body, category
FROM support_tickets
WHERE closed_at IS NULL
  AND resolution_summary IS NULL
  AND body NOT LIKE :divider
ORDER BY created_at, id
"""
)
_MERGE = text("UPDATE support_tickets SET body = :body WHERE id = :id")
_DELETE = text("DELETE FROM support_tickets WHERE id = :id")


def _first_line(body: str) -> str:
    return body.split("\n", 1)[0].strip()


def _pairs(rows):
    """One-to-one (copy, original) pairs by (tenant, category, subject, reporter,
    day) whose bodies open with the same line, each side in creation order.
    Rows are (id, tenant, subject, user, day, body, category). Returns the
    pairs and the copies that found no original."""
    originals: dict[tuple, list] = {}
    copies: list = []
    for row in rows:
        key = (row[1], row[6], row[2], row[3], str(row[4]))
        if row[5].endswith(_TRAILER):
            copies.append((key, row))
        else:
            originals.setdefault(key, []).append(row)
    out, unmatched = [], []
    for key, copy in copies:
        pool = originals.get(key) or []
        match = next((o for o in pool if _first_line(o[5]) == _first_line(copy[5])), None)
        if match is None:
            unmatched.append(copy)
            continue
        pool.remove(match)
        out.append((copy, match))
    return out, unmatched


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("support_tickets"):
        return
    rows = bind.execute(_ROWS, {"divider": "%" + _DIVIDER.strip() + "%"}).fetchall()
    pairs, unmatched = _pairs(rows)
    for copy, original in pairs:
        folded = original[5] + _DIVIDER + copy[5][: -len(_TRAILER)]
        bind.execute(_MERGE, {"body": folded, "id": original[0]})
        bind.execute(_DELETE, {"id": copy[0]})
        log.info("090: folded copied ticket %s into original %s", copy[0], original[0])
    for copy in unmatched:
        log.info("090: copied ticket %s has no open original to fold into — left as is", copy[0])
    log.info("090: %d copied bug report(s) folded into their originals, %d left as is", len(pairs), len(unmatched))


def downgrade() -> None:
    """No-op: the folded rows' text is on the originals that remain."""
