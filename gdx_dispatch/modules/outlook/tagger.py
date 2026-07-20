"""Sprint Outlook Integration — Phase 3 tagging engine.

Three strategies run in priority order (configurable per tenant via
``OutlookSettings.tag_strategy_order`` + ``tag_strategy_enabled``):

1. **auto_match** — sender/recipient email address → ``Customer.email_hash``
   exact match. Cheapest; fires first. Confidence = 1.00.

2. **job_thread** — regex on subject for ``[Job #N]`` or ``Re: estimate N``
   patterns. Confidence = 0.95.

3. **ai** — prompt Anthropic Haiku with subject + first 500 chars of body
   + recent customers/jobs. Confidence = whatever the model returns.
   Only runs if confidence ≥ ``OutlookSettings.ai_tag_threshold`` (default
   0.85). Uses the existing tenant Anthropic key (sprint 1.x ``key_storage``).

The orchestrator function ``tag_message(message, tenant_db, control_db)``
is idempotent — re-tagging an already-tagged message does nothing unless
the user manually cleared ``tag_strategy``.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from gdx_dispatch.core.pii import HashColumn
from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.modules.outlook.models import OutlookMessage, OutlookSettings


log = logging.getLogger("gdx_dispatch.modules.outlook.tagger")


# ── strategy results ───────────────────────────────────────────────────


class TagResult:
    """Output of a strategy. Either matches a customer/job or returns None."""
    def __init__(
        self,
        *,
        customer_id=None,
        job_id=None,
        confidence: Decimal = Decimal("1.00"),
        strategy: str = "",
    ):
        self.customer_id = customer_id
        self.job_id = job_id
        self.confidence = confidence
        self.strategy = strategy


# ── strategies ─────────────────────────────────────────────────────────


def _candidate_addresses(message: OutlookMessage) -> Iterable[str]:
    """Iterate all email addresses on this message — from + to + cc + bcc."""
    if message.from_address:
        yield message.from_address
    for collection in (message.to_addresses, message.cc_addresses, message.bcc_addresses):
        for addr in (collection or []):
            if addr:
                yield addr


def auto_match_strategy(
    message: OutlookMessage,
    tenant_db: Session,
) -> TagResult | None:
    """Match any sender/recipient email to a Customer.email_hash. First hit wins."""
    for addr in _candidate_addresses(message):
        h = HashColumn.hash_for_search(addr.lower().strip())
        customer = (
            tenant_db.query(Customer)
            .filter(Customer.email_hash == h, Customer.deleted_at.is_(None))
            .first()
        )
        if customer is not None:
            return TagResult(
                customer_id=customer.id,
                confidence=Decimal("1.00"),
                strategy="auto_match",
            )
    return None


_JOB_PATTERNS = [
    # Capture a UUID embedded in the subject — Job.id is a UUID in GDX.
    # `[Job #abcdef12-...]` or `Re: estimate <UUID>` style.
    re.compile(
        r"\[Job\s*#?\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bjob\s*#?\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
        re.IGNORECASE,
    ),
]


def job_thread_strategy(
    message: OutlookMessage,
    tenant_db: Session,
) -> TagResult | None:
    """Regex the subject for ``[Job #N]`` markers + look up matching Job.

    Job.id is a UUID; the regex matcher captures arbitrary digits/UUID-ish
    strings. We try to coerce the captured value to a UUID and match by id;
    if the captured value isn't a valid UUID we silently skip (the strategy
    is a safety net for explicit thread markers — auto_match covers the
    common case).
    """
    if not message.subject:
        return None
    from uuid import UUID
    for pat in _JOB_PATTERNS:
        m = pat.search(message.subject)
        if not m:
            continue
        captured = m.group(1)
        try:
            job_uuid = UUID(captured)
        except (ValueError, TypeError):
            log.debug("job_thread: subject pattern matched but token %r is not a UUID — skipping", captured)
            continue
        try:
            job = tenant_db.query(Job).filter(Job.id == job_uuid).first()
        except Exception:  # noqa: BLE001
            log.exception("job_thread: Job lookup failed for uuid=%s", job_uuid)
            job = None
        if job is not None:
            return TagResult(
                job_id=job.id,
                customer_id=job.customer_id,
                confidence=Decimal("0.95"),
                strategy="job_thread",
            )
    return None


def ai_strategy(
    message: OutlookMessage,
    tenant_db: Session,
    control_db: Session,
    *,
    threshold: Decimal = Decimal("0.85"),
) -> TagResult | None:
    """Ask Anthropic to tag the message. Only fires if confidence ≥ threshold.

    Stub for now — the full AI strategy ships in a follow-up slice. The
    orchestrator below skips this strategy when it returns None, so the
    pipeline still works end-to-end.
    """
    log.debug("ai_strategy: not yet implemented (returning None)")
    return None


# ── orchestrator ───────────────────────────────────────────────────────


_STRATEGY_FUNCS = {
    "auto_match": auto_match_strategy,
    "job_thread": job_thread_strategy,
    # ai handled separately because it needs control_db + threshold
}


def tag_message(
    message: OutlookMessage,
    tenant_db: Session,
    control_db: Session | None = None,
    *,
    settings: "OutlookSettings | None" = None,
) -> bool:
    """Apply the configured strategy chain to a message. Mutates the message
    in place (sets linked_customer_id, linked_job_id, tag_strategy, tag_confidence).
    Returns True if any strategy matched. Idempotent.

    ``settings`` may be pre-loaded by a batch caller (sync / backfill) to avoid
    re-querying OutlookSettings once per message — pass ``_load_tag_settings``.
    Omit it and the row is loaded here (per-call), which is fine for one-offs.
    """
    if message.tag_strategy:
        return False  # already tagged

    if settings is None:
        settings = tenant_db.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
    if settings is None:
        order = ["auto_match", "job_thread", "ai"]
        enabled = {"auto_match": True, "job_thread": True, "ai": True}
        threshold = Decimal("0.85")
    else:
        order = settings.tag_strategy_order or ["auto_match", "job_thread", "ai"]
        enabled = settings.tag_strategy_enabled or {"auto_match": True, "job_thread": True, "ai": True}
        threshold = settings.ai_tag_threshold or Decimal("0.85")

    for strategy_name in order:
        if not enabled.get(strategy_name, False):
            continue
        if strategy_name == "ai":
            if control_db is None:
                continue
            result = ai_strategy(message, tenant_db, control_db, threshold=threshold)
        else:
            fn = _STRATEGY_FUNCS.get(strategy_name)
            if fn is None:
                continue
            result = fn(message, tenant_db)
        if result is None:
            continue
        # AI strategy enforces its own threshold; non-AI strategies are confident-by-design.
        message.linked_customer_id = result.customer_id
        message.linked_job_id = result.job_id
        message.tag_strategy = result.strategy
        message.tag_confidence = result.confidence
        return True
    return False


def manual_tag(
    message: OutlookMessage,
    *,
    customer_id=None,
    job_id=None,
) -> None:
    """User-driven tag override. Bypasses strategy chain. Confidence = 1.00."""
    message.linked_customer_id = customer_id
    message.linked_job_id = job_id
    message.tag_strategy = "manual"
    message.tag_confidence = Decimal("1.00")
