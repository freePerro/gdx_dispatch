"""Detect recurring payments from synced bank-feed data.

QBO's ``RecurringTransaction`` entity is a *template* — what the user set
up, not what actually cleared the bank. Most fixed monthly outflows
(Phone.com debit, loan ACH, insurance premiums, SaaS subs) never get
templated, so the Forecasting "Recurring" card under-projects the real
operating burn.

This module is the counterpart: it scans ``qb_bank_transactions`` for
patterns that fit the industry "matured recurring" definition (Plaid /
Subaio / Ntropy): ≥3 occurrences, same merchant, consistent amount, at
a discoverable cadence. Each pattern becomes a ``RecurringStream`` row
in ``status='suggested'`` for the user to confirm or dismiss.

The detector is pure-read against ``qb_bank_transactions`` and only
upserts into ``recurring_streams`` / ``recurring_stream_hits`` — never
writes back to QuickBooks. Idempotent: re-running it just refreshes
``last_observed_date`` / ``occurrences_seen`` / ``next_expected_date``
and attaches any newly-synced hits, all guarded by the
``UNIQUE(stream_id, qb_txn_id)`` constraint.
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.forecasting.models import (
    CADENCE_ANNUAL,
    CADENCE_BIWEEKLY,
    CADENCE_MONTHLY,
    CADENCE_QUARTERLY,
    CADENCE_SEMIANNUAL,
    CADENCE_WEEKLY,
    STREAM_SOURCE_OBSERVED,
    STREAM_STATUS_SUGGESTED,
    RecurringStream,
    RecurringStreamHit,
)
from gdx_dispatch.modules.quickbooks.banking import QBBankTransaction

log = logging.getLogger(__name__)

# Tuning knobs (kept module-level for testability + Doug-tunable)
DETECTOR_WINDOW_MONTHS = 24
MIN_OCCURRENCES = 3
MAX_CV = 0.25  # coefficient of variation — Plaid-equivalent "matured" threshold
AMOUNT_BUCKET_TOLERANCE_PCT = 0.20  # ±20% of median amount

# Cadence classification by median day-delta between consecutive hits.
# Wide bands tolerate posting variance (weekends, ACH lag, holiday slips).
_CADENCE_BANDS: list[tuple[int, int, str]] = [
    (5, 9, CADENCE_WEEKLY),
    (12, 16, CADENCE_BIWEEKLY),
    (25, 35, CADENCE_MONTHLY),
    (80, 100, CADENCE_QUARTERLY),
    (170, 195, CADENCE_SEMIANNUAL),
    (350, 380, CADENCE_ANNUAL),
]

# Real GDX qb_bank_transactions.payee is already QBO-cleaned — values look
# like "Phone.com", "Amazon", "Midwest Bank". The noise-prefix garbage
# ("DBT CRD 0925 ...", "AUTOMATIC PAYMENT TO LOAN ACCT NO. …") lives in
# the MEMO field, not PAYEE. So normalize_payee is mostly a no-op for
# well-curated tenants, but we still strip the prefixes + transaction-id
# fragments as defense-in-depth for tenants whose QB connection hasn't
# categorized the bank feed yet (raw memo flows into payee in those
# cases).
_NOISE_PREFIX_PATTERNS = [
    re.compile(r"^DBT\s+CRD\s+\d+\s+\d+\s+", re.IGNORECASE),
    re.compile(r"^ATM\s+RCR\s+PAYMENT\s+", re.IGNORECASE),
    re.compile(r"^POS\s+DEB\s+\d+\s+\d+\s+", re.IGNORECASE),
    re.compile(r"^ACH\s+(PMT|PAYMENT)\s+", re.IGNORECASE),
    re.compile(r"^DEBIT\s+CARD\s+(DEBIT|PREAUTH)\s+", re.IGNORECASE),
    re.compile(r"^AUTOMATIC\s+(LOAN\s+)?PAYMENT\s+(TO\s+LOAN\s+ACCT\s+NO\.\s+)?", re.IGNORECASE),
]
# Strip transaction-id fragments anywhere: #16374977, *XXXX2000, C#9043,
# M2890 (single-letter prefix is the bank-statement check-ref convention).
_TXN_ID_FRAGMENT = re.compile(r"[A-Z]?[#*]+\s*[A-Z]?\d+", re.IGNORECASE)
# Strip standalone numeric runs of 5+ digits (transaction sequence numbers).
_LONG_DIGIT_RUN = re.compile(r"\b\d{5,}\b")
# Strip orphaned single-character tokens left behind after txn-id stripping
# (* and single letters that used to anchor a card ref). Match `*` or a
# single A-Z preceded by whitespace or string start and followed by
# whitespace or string end. Run AFTER the fragment strip so we don't eat
# parts of real merchant names.
_ORPHAN_SHORT_TOKEN = re.compile(r"(?:^|(?<=\s))(?:\*+|[A-Z])(?=\s|$)", re.IGNORECASE)
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_payee(raw: str | None) -> str:
    """Collapse bank-feed payee noise into a stable grouping key.

    Real GDX example (QBO-curated, clean):
      "Phone.com" → "PHONE.COM"
      "Alexandria Tools and More" → "ALEXANDRIA TOOLS AND MORE"

    Dirty bank-feed example (uncategorized tenant):
      "DBT CRD 0925 55200916 PHONE.COM #16374977 PHONE.COM CA C#9043"
        → noise prefix stripped → "PHONE.COM #16374977 PHONE.COM CA C#9043"
        → txn-id fragments stripped → "PHONE.COM  PHONE.COM CA"
        → whitespace collapsed + upper → "PHONE.COM PHONE.COM CA"

    Returns "" when the input would normalize to empty (caller skips those).
    """
    if not raw:
        return ""
    s = raw.strip()
    for pat in _NOISE_PREFIX_PATTERNS:
        new = pat.sub("", s)
        if new != s:
            s = new
            break
    s = _TXN_ID_FRAGMENT.sub("", s)
    s = _LONG_DIGIT_RUN.sub("", s)
    s = _ORPHAN_SHORT_TOKEN.sub("", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s.upper()


@dataclass
class StreamCandidate:
    payee_norm: str
    median_amount: float
    amount_min: float
    amount_max: float
    cadence: str
    occurrences: int
    first_seen: date
    last_seen: date
    next_expected: date | None
    txn_ids: list[tuple[str, date, float]]  # (qb_txn_id, txn_date, amount)


def _classify_cadence(median_delta_days: float) -> str | None:
    for lo, hi, name in _CADENCE_BANDS:
        if lo <= median_delta_days <= hi:
            return name
    return None


def _coefficient_of_variation(amounts: list[float]) -> float:
    if len(amounts) < 2:
        return 0.0
    mean = statistics.fmean(amounts)
    if mean == 0:
        return float("inf")
    sd = statistics.pstdev(amounts)
    return sd / mean


def _bucket_amounts(amounts: list[float]) -> dict[float, list[int]]:
    """Group amount indices into clusters where any pair stays within
    ``AMOUNT_BUCKET_TOLERANCE_PCT`` of the cluster's running median.

    Returns ``{cluster_median: [original_indices]}``. Used to keep an
    insurance premium that drifted from $115.40 → $115.67 in the same
    stream while keeping a $1,395 sub separate from a $15 small-fee.
    """
    clusters: dict[float, list[int]] = {}
    for idx, amt in enumerate(amounts):
        matched = False
        for med, members in list(clusters.items()):
            if abs(amt - med) <= AMOUNT_BUCKET_TOLERANCE_PCT * med:
                members.append(idx)
                # update cluster center
                new_med = statistics.median(amounts[i] for i in members)
                if new_med != med:
                    clusters[new_med] = clusters.pop(med)
                matched = True
                break
        if not matched:
            clusters[amt] = [idx]
    return clusters


def _project_next_date(last_date: date, cadence: str, anchor_day: int | None) -> date | None:
    """Roll the last-observed date forward by one cadence interval."""
    if cadence == CADENCE_WEEKLY:
        return last_date + timedelta(days=7)
    if cadence == CADENCE_BIWEEKLY:
        return last_date + timedelta(days=14)
    if cadence == CADENCE_MONTHLY:
        # add ~30 days; anchor_day refinement happens when we have a confirmed anchor
        return last_date + timedelta(days=30)
    if cadence == CADENCE_QUARTERLY:
        return last_date + timedelta(days=91)
    if cadence == CADENCE_SEMIANNUAL:
        return last_date + timedelta(days=182)
    if cadence == CADENCE_ANNUAL:
        return last_date + timedelta(days=365)
    return None


def find_candidates(db: Session, *, today: date | None = None) -> list[StreamCandidate]:
    """Scan ``qb_bank_transactions`` and return candidate recurring streams.

    Pure-read. No DB writes. Callers (``upsert_streams``) apply the writes.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=DETECTOR_WINDOW_MONTHS * 31)
    rows = (
        db.query(QBBankTransaction)
        .filter(QBBankTransaction.txn_date.isnot(None))
        .filter(QBBankTransaction.txn_date >= cutoff)
        .filter(QBBankTransaction.payee.isnot(None))
        # Tombstoned txns must not resurface as recurring suggestions.
        # Every other reader in banking.py applies this; the detector must too.
        .filter(QBBankTransaction.deleted_at.is_(None))
        .all()
    )

    # Group by normalized payee first, then split each by amount-cluster.
    by_payee: dict[str, list[QBBankTransaction]] = defaultdict(list)
    for r in rows:
        key = normalize_payee(r.payee)
        if not key:
            continue
        by_payee[key].append(r)

    candidates: list[StreamCandidate] = []
    for payee_norm, txns in by_payee.items():
        if len(txns) < MIN_OCCURRENCES:
            continue
        amounts = [float(t.amount or 0) for t in txns]
        for med, idxs in _bucket_amounts(amounts).items():
            if len(idxs) < MIN_OCCURRENCES:
                continue
            members = [txns[i] for i in idxs]
            member_amounts = [float(m.amount or 0) for m in members]
            cv = _coefficient_of_variation(member_amounts)
            if cv > MAX_CV:
                continue
            # Sort by date for cadence inference + first/last/next
            members.sort(key=lambda m: m.txn_date)
            dates = [m.txn_date for m in members]
            deltas = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
            median_delta = float(statistics.median(deltas)) if deltas else 0.0
            cadence = _classify_cadence(median_delta)
            if cadence is None:
                continue
            # Distinct months gate — protects against a vendor with 5 hits in one week
            months_hit = len({(d.year, d.month) for d in dates})
            if months_hit < MIN_OCCURRENCES:
                continue
            first_seen, last_seen = dates[0], dates[-1]
            next_expected = _project_next_date(last_seen, cadence, None)
            amt_min = round(med * (1 - AMOUNT_BUCKET_TOLERANCE_PCT), 2)
            amt_max = round(med * (1 + AMOUNT_BUCKET_TOLERANCE_PCT), 2)
            candidates.append(StreamCandidate(
                payee_norm=payee_norm,
                median_amount=round(med, 2),
                amount_min=amt_min,
                amount_max=amt_max,
                cadence=cadence,
                occurrences=len(members),
                first_seen=first_seen,
                last_seen=last_seen,
                next_expected=next_expected,
                txn_ids=[(m.qb_txn_id, m.txn_date, float(m.amount or 0)) for m in members],
            ))
    return candidates


def upsert_streams(db: Session, candidates: Iterable[StreamCandidate]) -> dict[str, int]:
    """Apply detector candidates to ``recurring_streams`` + hits.

    For each candidate:
    - Find existing stream by (payee_pattern, status active/suggested) where
      amount window overlaps. If found → refresh observation fields.
    - Otherwise → insert as ``source=observed, status=suggested``.
    - Attach hits via ``RecurringStreamHit``; uniqueness prevents dupes on retry.

    Returns counts: ``{"inserted": N, "updated": N, "hits_added": N}``.
    """
    inserted = updated = hits_added = 0
    for cand in candidates:
        # .first() not .one_or_none() — a user can manually create overlapping
        # streams (e.g. split a Phone.com $44 into a "phone" and "fax" stream
        # with overlapping windows). Auditor flagged the MultipleResultsFound
        # crash. Tie-breaker is insertion order (deterministic enough — the
        # most-recently-created or most-recently-active match wins; refining
        # this is future work if a real tenant hits ambiguity in the UI).
        existing = (
            db.query(RecurringStream)
            .filter(RecurringStream.payee_pattern == cand.payee_norm)
            .filter(RecurringStream.status.in_([STREAM_STATUS_SUGGESTED, "active"]))
            .filter(RecurringStream.amount_min <= cand.median_amount)
            .filter(RecurringStream.amount_max >= cand.median_amount)
            .order_by(RecurringStream.created_at.desc())
            .first()
        )
        if existing is None:
            stream = RecurringStream(
                label=cand.payee_norm.title(),
                source=STREAM_SOURCE_OBSERVED,
                status=STREAM_STATUS_SUGGESTED,
                payee_pattern=cand.payee_norm,
                amount_min=cand.amount_min,
                amount_max=cand.amount_max,
                cadence=cand.cadence,
                occurrences_seen=cand.occurrences,
                start_date=cand.first_seen,
                last_observed_date=cand.last_seen,
                next_expected_date=cand.next_expected,
            )
            db.add(stream)
            db.flush()
            inserted += 1
            stream_id = stream.id
        else:
            existing.occurrences_seen = cand.occurrences
            existing.last_observed_date = cand.last_seen
            existing.next_expected_date = cand.next_expected
            # Don't bump cadence on an active stream — user may have set it
            # deliberately. Only mutate cadence on a still-suggested row.
            if existing.status == STREAM_STATUS_SUGGESTED:
                existing.cadence = cand.cadence
            db.flush()
            updated += 1
            stream_id = existing.id

        # Pre-fetch already-attached qb_txn_ids for this stream so we don't
        # rely on IntegrityError-and-rollback (which would nuke the whole
        # transaction). Set-based existence check is portable across SQLite + PG.
        existing_txn_ids = set(
            db.scalars(
                select(RecurringStreamHit.qb_txn_id).where(
                    RecurringStreamHit.stream_id == stream_id
                )
            ).all()
        )
        for qb_txn_id, txn_date, amount in cand.txn_ids:
            if qb_txn_id in existing_txn_ids:
                continue
            db.add(RecurringStreamHit(
                stream_id=stream_id,
                qb_txn_id=qb_txn_id,
                txn_date=txn_date,
                amount=amount,
                confirmed=False,
            ))
            existing_txn_ids.add(qb_txn_id)
            hits_added += 1
        db.flush()
    db.commit()
    return {"inserted": inserted, "updated": updated, "hits_added": hits_added}


def run_detector(db: Session, *, today: date | None = None) -> dict[str, int]:
    """Convenience: find + upsert in one call. Returns the upsert counts."""
    cands = find_candidates(db, today=today)
    stats = upsert_streams(db, cands)
    log.info("observed_recurring detector run: %s", stats)
    return stats
