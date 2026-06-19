"""Weekly PAT lifecycle sweep (SS-14 slice G).

# INTEGRATION TODO: schedule this module as a Celery beat task or cron
# entry during SS-14 integration. The tool exposes pure functions so
# it is unit-testable without a Celery scheduler.

Policy (D-15 GitHub-style):

    * A PAT unused for ``UNUSED_FLAG_DAYS`` (default 30) is FLAGGED:
      an event is emitted so the owner can be notified, but the token
      remains valid. The flag is idempotent — re-running the sweep does
      not re-emit if a ``flagged_at`` marker has already been written.
    * A PAT unused for ``UNUSED_DELETE_DAYS`` (default 365) is AUTO-
      REVOKED: ``revoked_at`` is set and an event is emitted.
    * A PAT past its ``expires_at`` is AUTO-REVOKED the same way.

"Unused" means either ``last_used_at`` is old OR (if the token has
never been used) ``created_at`` is old. Using created_at as the fall-
back prevents freshly-minted tokens that never get used from sitting
forever.

The sweep operates on the platform control DB and commits once per
batch. Because ``flagged_at`` is not a column on AccessToken today,
the flag marker is stored in the audit/event layer via ``emit_event``
— the re-run idempotency check is "is there already a
``gdx.pat.flagged_unused.v1`` event for this pat_id within the last
90 days?". Once an ``AccessToken.flagged_unused_at`` column lands
(integration TODO in platform_ss14_additions.py), the check becomes
a column read.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.events import emit_event
from gdx_dispatch.models.platform_extensions import AccessToken, EventOutbox

log = logging.getLogger(__name__)


UNUSED_FLAG_DAYS = 30
UNUSED_DELETE_DAYS = 365
FLAG_DEDUP_WINDOW_DAYS = 90  # don't re-emit a flag event within this window


@dataclass
class SweepResult:
    scanned: int = 0
    flagged: int = 0
    revoked_unused: int = 0
    revoked_expired: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "flagged": self.flagged,
            "revoked_unused": self.revoked_unused,
            "revoked_expired": self.revoked_expired,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _last_activity(pat: AccessToken) -> datetime:
    """Most-recent of last_used_at / created_at, tz-aware."""
    lu = _ensure_aware(pat.last_used_at)
    cr = _ensure_aware(pat.created_at) or _now()
    return lu if lu is not None and lu > cr else cr


def _recent_flag_exists(db: Session, pat_id: UUID, now: datetime) -> bool:
    """Has a flag event been emitted for this PAT recently?"""
    window_start = now - timedelta(days=FLAG_DEDUP_WINDOW_DAYS)
    pat_id_str = str(pat_id)
    rows = db.execute(
        select(EventOutbox).where(
            and_(
                EventOutbox.event_name == "gdx_dispatch.pat.flagged_unused.v1",
                EventOutbox.emitted_at >= window_start,
            )
        )
    ).scalars()
    for row in rows:
        payload = row.payload or {}
        if str(payload.get("pat_id")) == pat_id_str:
            return True
    return False


def sweep(
    db: Session,
    *,
    now: datetime | None = None,
    unused_flag_days: int = UNUSED_FLAG_DAYS,
    unused_delete_days: int = UNUSED_DELETE_DAYS,
) -> SweepResult:
    """Run one lifecycle pass over non-revoked PATs. Commits at the end."""
    result = SweepResult()
    current = _ensure_aware(now) or _now()
    flag_threshold = current - timedelta(days=unused_flag_days)
    delete_threshold = current - timedelta(days=unused_delete_days)

    # Candidate set: only non-revoked rows with a PAT prefix.
    # Filter in Python for portable tz-handling on SQLite.
    stmt = select(AccessToken).where(
        and_(
            AccessToken.revoked_at.is_(None),
            or_(
                AccessToken.prefix == "gdx_pat_live_",
                AccessToken.prefix == "gdx_pat_test_",
                AccessToken.prefix == "gdx_sk_live_",
                AccessToken.prefix == "gdx_sk_test_",
            ),
        )
    )
    candidates = list(db.execute(stmt).scalars())

    for pat in candidates:
        result.scanned += 1
        tenant_id = _tenant_id_for(db, pat)

        # 1) Expired → auto-revoke.
        expires_at = _ensure_aware(pat.expires_at)
        if expires_at is not None and expires_at < current:
            pat.revoked_at = current
            emit_event(
                db,
                "gdx_dispatch.pat.auto_revoked.v1",
                {
                    "pat_id": str(pat.id),
                    "tenant_id": tenant_id,
                    "reason": "expired",
                    "expires_at": expires_at.isoformat(),
                },
                tenant_id=tenant_id,
            )
            result.revoked_expired += 1
            continue

        activity = _last_activity(pat)

        # 2) Unused > delete threshold → auto-revoke.
        if activity < delete_threshold:
            pat.revoked_at = current
            emit_event(
                db,
                "gdx_dispatch.pat.auto_revoked.v1",
                {
                    "pat_id": str(pat.id),
                    "tenant_id": tenant_id,
                    "reason": "unused_over_threshold",
                    "unused_since": activity.isoformat(),
                    "threshold_days": unused_delete_days,
                },
                tenant_id=tenant_id,
            )
            result.revoked_unused += 1
            continue

        # 3) Unused > flag threshold → flag (dedup within window).
        if activity < flag_threshold and not _recent_flag_exists(db, pat.id, current):
            emit_event(
                db,
                "gdx_dispatch.pat.flagged_unused.v1",
                {
                    "pat_id": str(pat.id),
                    "tenant_id": tenant_id,
                    "unused_since": activity.isoformat(),
                    "threshold_days": unused_flag_days,
                },
                tenant_id=tenant_id,
            )
            result.flagged += 1

    db.commit()
    return result


def _tenant_id_for(db: Session, pat: AccessToken) -> str | None:
    """Best-effort tenant lookup for event fan-out.

    Uses the PAT's installation tenant if present, else None. We do NOT
    resolve via membership here — the cron runs out-of-band and can
    tolerate a ``None`` tenant_id on the emitted event; downstream
    consumers treat it as a global/system event.
    """
    if pat.installation_id is None:
        return None
    from gdx_dispatch.models.platform_extensions import Installation

    install = db.get(Installation, pat.installation_id)
    return install.tenant_id if install else None


def main() -> int:  # pragma: no cover
    """CLI entry point; intended for ad-hoc ops runs."""
    import argparse
    import json

    from gdx_dispatch.core.database import SessionLocal

    parser = argparse.ArgumentParser()
    parser.add_argument("--flag-days", type=int, default=UNUSED_FLAG_DAYS)
    parser.add_argument("--delete-days", type=int, default=UNUSED_DELETE_DAYS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    db: Session = SessionLocal()
    try:
        result = sweep(
            db,
            unused_flag_days=args.flag_days,
            unused_delete_days=args.delete_days,
        )
    finally:
        db.close()

    print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# Type hint alias kept for external type checkers; suppresses "unused
# import" if consumers check this file for the symbol.
_: Any = None
