"""Bank statement import service — tie-out gate, overlap integrity, evidence
insert, void-by-batch.

The gate philosophy (docs/design/bank-statement-import-plan.md §5): a
statement either ties out completely — balance equation, summary counts and
totals, daily-balance recompute at the bank's listed dates, ending equation —
or it contributes NOTHING to the evidence table. A failed import keeps its
row, its report, and its file (the diagnosis artifact) but inserts no lines.

The tie-out is arithmetic; it cannot see description/wrap fidelity. That is
the overlap-integrity check's job: where the new statement's period
intersects an existing passed import, both statements claim full coverage of
the intersection, so the line-hash sets over that window must match
BIDIRECTIONALLY. A (date, amount) match with a hash mismatch is description
drift — a wrap-gluing defect — and fails the import loudly (audit §11.2).

``line_hash`` = sha256(institution|last4|date|amount|normalized ANCHOR
line|occurrence_n). Anchor only — never the glued wrap lines — so dedup
across overlapping statements is robust to wrap differences. Deterministic
from the evidence itself (no DB ids), so hashes are reproducible across
environments.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.bank_feeds.statement_models import (
    TIE_OUT_FAILED,
    TIE_OUT_PASSED,
    BankAccount,
    BankStatementImport,
    BankStatementLine,
    BankStatementLineImage,
    BankStatementLineSource,
)
from gdx_dispatch.modules.bank_feeds.statement_parsers import community_bank
from gdx_dispatch.modules.bank_feeds.statement_parsers.community_bank import (
    SECTION_CHECK,
    SECTION_DEBIT,
    SECTION_DEPOSIT,
    SECTION_INTEREST,
    SECTION_SERVICE_CHARGE,
    ParsedStatement,
    ParsedTxn,
    StatementParseError,
    StatementStructureError,
)

__all__ = [
    "import_statement",
    "void_import",
    "StatementParseError",
    "StatementStructureError",
]

log = logging.getLogger(__name__)

MAX_STATEMENT_BYTES = 10 * 1024 * 1024

_WS = re.compile(r"\s+")


def _imports_dir() -> Path:
    # Sibling of the Banno-fetched archive ($UPLOAD_DIR/bank_statements).
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads/")) / "bank_statements" / "imports"


def _normalize_anchor(anchor: str) -> str:
    return _WS.sub(" ", anchor).strip().lower()


def compute_line_hash(
    institution: str, last4: str, txn_date: date, amount_cents: int,
    anchor: str, occurrence_n: int,
) -> str:
    key = f"{institution}|{last4}|{txn_date.isoformat()}|{amount_cents}|{_normalize_anchor(anchor)}|{occurrence_n}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _hashed_lines(parsed: ParsedStatement) -> list[tuple[ParsedTxn, int, str]]:
    """Assign occurrence_n (1-based, statement order, per identical
    (date, amount, anchor) key) and the line hash to every parsed txn."""
    seen: dict[tuple, int] = {}
    out = []
    for txn in parsed.lines:
        key = (txn.txn_date, txn.amount_cents, _normalize_anchor(txn.anchor))
        occurrence = seen.get(key, 0) + 1
        seen[key] = occurrence
        out.append((txn, occurrence, compute_line_hash(
            community_bank.INSTITUTION, parsed.last4, txn.txn_date,
            txn.amount_cents, txn.anchor, occurrence,
        )))
    return out


# ── tie-out ────────────────────────────────────────────────────────────


def _check(name: str, ok: bool, expected, actual, detail: str = "") -> dict:
    entry = {"name": name, "ok": bool(ok), "expected": expected, "actual": actual}
    if detail:
        entry["detail"] = detail
    return entry


def compute_tie_out(parsed: ParsedStatement) -> tuple[str, dict]:
    """Run every arithmetic invariant on the PARSED rows (never inserted
    ones — a fully-deduped restatement still validates end to end)."""
    checks: list[dict] = []

    dep = [t for t in parsed.lines if t.section == SECTION_DEPOSIT]
    deb = [t for t in parsed.lines if t.section in (SECTION_DEBIT, SECTION_CHECK)]
    sc = [t for t in parsed.lines if t.section == SECTION_SERVICE_CHARGE]
    intr = [t for t in parsed.lines if t.section == SECTION_INTEREST]

    eq = (parsed.beginning_cents + parsed.deposits_total_cents + parsed.interest_cents
          - parsed.debits_total_cents - parsed.service_charge_cents)
    checks.append(_check("summary_equation", eq == parsed.ending_cents, parsed.ending_cents, eq))

    # Summary counts/totals EXCLUDE interest and the service charge; checks
    # from CHECKS IN NUMBER ORDER count toward debits. A summary without a
    # printed count (savings zero-activity form) asserts totals only.
    if parsed.deposits_count is not None:
        checks.append(_check("deposits_count", parsed.deposits_count == len(dep),
                             parsed.deposits_count, len(dep)))
    checks.append(_check("deposits_total", sum(t.amount_cents for t in dep) == parsed.deposits_total_cents,
                         parsed.deposits_total_cents, sum(t.amount_cents for t in dep)))
    if parsed.debits_count is not None:
        checks.append(_check("debits_count", parsed.debits_count == len(deb),
                             parsed.debits_count, len(deb)))
    checks.append(_check("debits_total", -sum(t.amount_cents for t in deb) == parsed.debits_total_cents,
                         parsed.debits_total_cents, -sum(t.amount_cents for t in deb)))

    checks.append(_check("service_charge_rows", -sum(t.amount_cents for t in sc) == parsed.service_charge_cents,
                         parsed.service_charge_cents, -sum(t.amount_cents for t in sc)))
    if parsed.has_service_charge_detail:
        checks.append(_check("service_charge_detail",
                             parsed.service_charge_detail_cents == parsed.service_charge_cents,
                             parsed.service_charge_cents, parsed.service_charge_detail_cents))
    checks.append(_check("interest_rows", sum(t.amount_cents for t in intr) == parsed.interest_cents,
                         parsed.interest_cents, sum(t.amount_cents for t in intr)))

    # Daily-balance recompute AT THE BANK'S LISTED DATES — never by emitting
    # a row per transaction-active day: the table can open with a
    # period-start seed row on a day with no transactions (audit §11.3).
    daily_failures = []
    for d, bal in parsed.daily_balances:
        run = parsed.beginning_cents + sum(t.amount_cents for t in parsed.lines if t.txn_date <= d)
        if run != bal:
            daily_failures.append({"date": d.isoformat(), "expected": bal, "actual": run})
    checks.append(_check("daily_recompute", not daily_failures,
                         f"{len(parsed.daily_balances)} rows", daily_failures or "all match"))

    total = parsed.beginning_cents + sum(t.amount_cents for t in parsed.lines)
    checks.append(_check("ending_equation", total == parsed.ending_cents, parsed.ending_cents, total))

    status = TIE_OUT_PASSED if all(c["ok"] for c in checks) else TIE_OUT_FAILED
    return status, {"checks": checks}


def _continuity_warnings(db: Session, account: BankAccount, parsed: ParsedStatement) -> list[dict]:
    """Period-aware continuity (audit §11.3): compare against the statement
    whose period is ADJACENT (end + 1 day == start), never 'previous by
    date' — a quarterly restatement's predecessor is the statement before
    the restated window. Warning-level: out-of-order arrival and gaps are
    reported, not fatal."""
    warnings: list[dict] = []
    others = db.scalars(
        select(BankStatementImport).where(
            BankStatementImport.bank_account_id == account.id,
            BankStatementImport.voided_at.is_(None),
            BankStatementImport.tie_out_status == TIE_OUT_PASSED,
        )
    ).all()
    for other in others:
        if other.period_end + timedelta(days=1) == parsed.period_start \
                and other.ending_balance_cents != parsed.beginning_cents:
            warnings.append({
                "kind": "continuity_mismatch_predecessor",
                "other_period_end": other.period_end.isoformat(),
                "other_ending_cents": other.ending_balance_cents,
                "beginning_cents": parsed.beginning_cents,
            })
        if parsed.period_end + timedelta(days=1) == other.period_start \
                and parsed.ending_cents != other.beginning_balance_cents:
            warnings.append({
                "kind": "continuity_mismatch_successor",
                "other_period_start": other.period_start.isoformat(),
                "other_beginning_cents": other.beginning_balance_cents,
                "ending_cents": parsed.ending_cents,
            })
    return warnings


def _overlap_integrity(
    db: Session, account: BankAccount, parsed: ParsedStatement,
    hashed: list[tuple[ParsedTxn, int, str]],
) -> list[dict]:
    """Bidirectional evidence agreement over every period intersection with
    an existing passed import. Both statements claim full coverage of the
    intersection, so hash-set inequality there is always a defect: either
    description/wrap drift (same dates+amounts, different hashes) or a
    genuine restatement divergence. Returns failure entries; empty = pass."""
    failures: list[dict] = []
    others = db.scalars(
        select(BankStatementImport).where(
            BankStatementImport.bank_account_id == account.id,
            BankStatementImport.voided_at.is_(None),
            BankStatementImport.tie_out_status == TIE_OUT_PASSED,
        )
    ).all()
    for other in others:
        w_start = max(parsed.period_start, other.period_start)
        w_end = min(parsed.period_end, other.period_end)
        if w_start > w_end:
            continue
        existing = db.scalars(
            select(BankStatementLine)
            .join(BankStatementLineSource, BankStatementLineSource.line_id == BankStatementLine.id)
            .where(
                BankStatementLineSource.import_id == other.id,
                BankStatementLine.txn_date >= w_start,
                BankStatementLine.txn_date <= w_end,
            )
        ).all()
        existing_by_hash = {line.line_hash: line for line in existing}
        new_by_hash = {h: t for t, _o, h in hashed if w_start <= t.txn_date <= w_end}

        missing_from_new = set(existing_by_hash) - set(new_by_hash)
        missing_from_existing = set(new_by_hash) - set(existing_by_hash)
        if not missing_from_new and not missing_from_existing:
            continue

        # Diagnosis: (date, amount) present on both sides but hashes differ
        # = the wrap/description drift case the arithmetic gate cannot see.
        existing_da = {(existing_by_hash[h].txn_date.isoformat(), existing_by_hash[h].amount_cents)
                       for h in missing_from_new}
        drift = [
            {"date": t.txn_date.isoformat(), "amount_cents": t.amount_cents, "anchor": t.anchor}
            for h, t in new_by_hash.items()
            if h in missing_from_existing
            and (t.txn_date.isoformat(), t.amount_cents) in existing_da
        ]
        failures.append({
            "other_import_id": str(other.id),
            "window": [w_start.isoformat(), w_end.isoformat()],
            "existing_unmatched": len(missing_from_new),
            "new_unmatched": len(missing_from_existing),
            "description_drift": drift,
            # There is deliberately no force flag: the recovery path for a
            # legitimate drift (bank re-render, extractor version change) is
            # void the conflicting import(s), then re-import both files —
            # the partial sha index re-admits a voided import's bytes.
            "recovery": "void conflicting import(s), then re-import both files",
        })
    return failures


# ── import / void ──────────────────────────────────────────────────────


def _get_or_create_account(db: Session, parsed: ParsedStatement) -> BankAccount:
    account = db.scalars(
        select(BankAccount).where(
            BankAccount.institution == community_bank.INSTITUTION,
            BankAccount.last4 == parsed.last4,
        )
    ).first()
    if account is None:
        account = BankAccount(
            name=parsed.account_title.title() or f"Account x{parsed.last4}",
            kind=parsed.kind,
            institution=community_bank.INSTITUTION,
            last4=parsed.last4,
        )
        db.add(account)
        db.flush()
    return account


def import_statement(
    db: Session, pdf_bytes: bytes, filename: str, imported_by: str | None = None,
) -> dict:
    """Import one statement PDF. Commits on success paths; every outcome is
    a structured result (multi-file uploads report per file):

    - ``duplicate_file``  — identical bytes already imported (non-voided)
    - ``parse_error``     — not this layout, or structure defeated the
                            parser (``flavor`` distinguishes; the latter is
                            a defect)
    - ``failed_tie_out``  — import row + report + file stored, NO lines
    - ``imported``        — evidence inserted/attested
    """
    if len(pdf_bytes) > MAX_STATEMENT_BYTES:
        return {"status": "parse_error", "flavor": "too_large", "filename": filename,
                "error": f"file exceeds {MAX_STATEMENT_BYTES} bytes"}

    sha = hashlib.sha256(pdf_bytes).hexdigest()
    existing = db.scalars(
        select(BankStatementImport).where(
            BankStatementImport.file_sha256 == sha,
            BankStatementImport.voided_at.is_(None),
        )
    ).first()
    if existing is not None:
        return {"status": "duplicate_file", "filename": filename,
                "import_id": str(existing.id), "tie_out_status": existing.tie_out_status}

    try:
        parsed = community_bank.parse_statement_pdf(pdf_bytes)
    except StatementStructureError as exc:
        return {"status": "parse_error", "flavor": "structure", "filename": filename, "error": str(exc)}
    except StatementParseError as exc:
        return {"status": "parse_error", "flavor": "not_recognized", "filename": filename, "error": str(exc)}

    account = _get_or_create_account(db, parsed)
    hashed = _hashed_lines(parsed)
    tie_out_status, report = compute_tie_out(parsed)

    if tie_out_status == TIE_OUT_PASSED:
        overlap_failures = _overlap_integrity(db, account, parsed, hashed)
        report["checks"].append(_check(
            "overlap_integrity", not overlap_failures,
            "bidirectional hash agreement over period intersections",
            overlap_failures or "no conflicts",
        ))
        if overlap_failures:
            tie_out_status = TIE_OUT_FAILED

    report["continuity_warnings"] = _continuity_warnings(db, account, parsed)

    imports_dir = _imports_dir()
    imports_dir.mkdir(parents=True, exist_ok=True)
    storage_path = imports_dir / f"import-{sha[:24]}.pdf"
    storage_path.write_bytes(pdf_bytes)

    imp = BankStatementImport(
        bank_account_id=account.id,
        file_sha256=sha,
        original_filename=filename[:255] if filename else None,
        storage_path=str(storage_path),
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        beginning_balance_cents=parsed.beginning_cents,
        ending_balance_cents=parsed.ending_cents,
        deposits_count=parsed.deposits_count,
        deposits_total_cents=parsed.deposits_total_cents,
        debits_count=parsed.debits_count,
        debits_total_cents=parsed.debits_total_cents,
        service_charge_cents=parsed.service_charge_cents,
        interest_cents=parsed.interest_cents,
        tie_out_status=tie_out_status,
        tie_out_report=report,
        imported_by=imported_by,
    )
    db.add(imp)
    db.flush()

    added = deduped = 0
    txn_line_ids: dict[int, object] = {}
    if tie_out_status == TIE_OUT_PASSED:
        for txn, occurrence, line_hash in hashed:
            line = db.scalars(
                select(BankStatementLine).where(
                    BankStatementLine.bank_account_id == account.id,
                    BankStatementLine.line_hash == line_hash,
                )
            ).first()
            if line is None:
                line = BankStatementLine(
                    bank_account_id=account.id,
                    import_id=imp.id,
                    txn_date=txn.txn_date,
                    amount_cents=txn.amount_cents,
                    description=txn.description,
                    section=txn.section,
                    check_number=txn.check_number,
                    occurrence_n=occurrence,
                    line_hash=line_hash,
                )
                db.add(line)
                db.flush()
                added += 1
            else:
                deduped += 1
            txn_line_ids[id(txn)] = line.id
            db.add(BankStatementLineSource(line_id=line.id, import_id=imp.id))
        imp.lines_added = added
        imp.lines_deduped = deduped

    image_stats = {"images_extracted": 0, "images_paired": 0}
    if tie_out_status == TIE_OUT_PASSED and parsed.captions:
        # Images are display metadata; a gallery failure (undecodable codec,
        # disk error, exotic color space) must NEVER veto evidence import.
        try:
            image_stats = _attach_images(db, imp, parsed, pdf_bytes, sha, txn_line_ids)
        except Exception:  # noqa: BLE001
            log.exception("statement image extraction failed; importing without gallery")
            image_stats = {"images_extracted": 0, "images_paired": 0, "images_error": True}

    db.commit()
    return {
        "status": "imported" if tie_out_status == TIE_OUT_PASSED else "failed_tie_out",
        "filename": filename,
        "import_id": str(imp.id),
        "account": {"id": str(account.id), "name": account.name, "last4": account.last4},
        "period_start": parsed.period_start.isoformat(),
        "period_end": parsed.period_end.isoformat(),
        "tie_out_status": tie_out_status,
        "lines_added": added,
        "lines_deduped": deduped,
        "continuity_warnings": report["continuity_warnings"],
        **image_stats,
    }


def _attach_images(
    db: Session, imp: BankStatementImport, parsed: ParsedStatement,
    pdf_bytes: bytes, sha: str, txn_line_ids: dict[int, object],
) -> dict:
    """Extract the check/deposit-ticket scans and pair them to evidence
    lines via the captions (empirically: one scan per caption, in order).

    Pairing is display metadata, never evidence — so it degrades instead of
    failing: a scan/caption count mismatch stores every scan gallery-only
    (line_id NULL), and a caption with no matching line keeps its scan in
    the gallery too. Written checks pair by check number; deposit tickets
    pair by (amount, full caption date), consuming candidates in order so
    identical same-day deposits pair one each.
    """
    try:
        pils = community_bank.extract_caption_images(pdf_bytes)
    except StatementParseError:
        pils = []
    if not pils:
        return {"images_extracted": 0, "images_paired": 0}

    img_dir = _imports_dir() / f"import-{sha[:24]}-img"
    img_dir.mkdir(parents=True, exist_ok=True)

    counts_match = len(pils) == len(parsed.captions)
    consumed: set[int] = set()

    def _line_for(caption) -> object | None:
        for txn in parsed.lines:
            if id(txn) in consumed or id(txn) not in txn_line_ids:
                continue
            if caption.check_no != "0":
                hit = txn.section == SECTION_CHECK and txn.check_number == caption.check_no
            else:
                hit = (txn.section == SECTION_DEPOSIT
                       and txn.amount_cents == caption.amount_cents
                       and txn.txn_date == caption.caption_date)
            if hit:
                consumed.add(id(txn))
                return txn_line_ids[id(txn)]
        return None

    paired = 0
    for i, pil in enumerate(pils):
        path = img_dir / f"img-{i:02d}.png"
        try:
            if pil.mode not in ("RGB", "L"):
                pil = pil.convert("RGB")  # PNG can't hold CMYK etc.
            pil.save(path)
        except OSError:
            log.exception("could not save statement image %s; skipping it", path)
            continue
        caption = parsed.captions[i] if counts_match else None
        line_id = _line_for(caption) if caption else None
        if line_id is not None:
            paired += 1
        db.add(BankStatementLineImage(
            import_id=imp.id,
            line_id=line_id,
            storage_path=str(path),
            caption_check_no=caption.check_no if caption else None,
            caption_amount_cents=caption.amount_cents if caption else None,
            caption_date=caption.caption_date if caption else None,
            sort_order=i,
        ))
    return {"images_extracted": len(pils), "images_paired": paired}


def void_import(db: Session, imp: BankStatementImport) -> dict:
    """Void by batch: delete THIS import's attestations, then only lines
    left with zero attestations — evidence co-vouched by a surviving
    overlapping import stays (audit §11.5; the real corpus contains the
    triggering pair). Surviving lines may keep a first-seen ``import_id``
    pointing at the voided batch; that column is provenance, not ownership.
    """
    from gdx_dispatch.core.audit import utcnow

    if imp.voided_at is not None:
        return {"status": "already_voided", "import_id": str(imp.id)}

    # ── read-only phase: decide everything BEFORE mutating anything ────
    attested_line_ids = list(db.scalars(
        select(BankStatementLineSource.line_id).where(BankStatementLineSource.import_id == imp.id)
    ).all())
    # A line dies with this void iff this import is its ONLY attestation.
    doomed_line_ids = [
        line_id for line_id in attested_line_ids
        if db.scalars(
            select(BankStatementLineSource).where(
                BankStatementLineSource.line_id == line_id,
                BankStatementLineSource.import_id != imp.id,
            )
        ).first() is None
    ]

    # Cross-PR seam (stack audit): PR 3's match children hold FKs to the
    # lines this void is about to delete. A CONFIRMED match is an office
    # assertion — never silently unlink it: refuse the whole void (before
    # any mutation) and make the human unconfirm first. Suggested/rejected
    # matches are derived data and are removed with their lines.
    from gdx_dispatch.modules.bank_feeds.statement_models import (
        MATCH_CONFIRMED,
        BankMatch,
        BankMatchExternal,
        BankMatchLine,
    )

    referencing = db.scalars(
        select(BankMatchLine).where(BankMatchLine.line_id.in_(doomed_line_ids))
    ).all() if doomed_line_ids else []
    confirmed_match_ids = {ml.match_id for ml in referencing if ml.match_status == MATCH_CONFIRMED}
    if confirmed_match_ids:
        raise ValueError(
            f"{len(confirmed_match_ids)} confirmed match(es) reference this "
            "statement's transactions — unconfirm them first, then void"
        )
    stale_match_ids = {ml.match_id for ml in referencing}

    # ── mutation phase ─────────────────────────────────────────────────
    # Images belong to the import that extracted them (a co-attesting
    # overlap import contributes its own rows). Collect paths now, DELETE
    # rows now, but unlink files only AFTER the commit succeeds — otherwise
    # a failed commit leaves rows pointing at deleted files (audit finding).
    image_paths: list[str] = []
    for image in db.scalars(
        select(BankStatementLineImage).where(BankStatementLineImage.import_id == imp.id)
    ).all():
        if image.storage_path:
            image_paths.append(image.storage_path)
        db.delete(image)

    for source in db.scalars(
        select(BankStatementLineSource).where(BankStatementLineSource.import_id == imp.id)
    ).all():
        db.delete(source)
    db.flush()

    if stale_match_ids:
        db.query(BankMatchLine).filter(
            BankMatchLine.match_id.in_(stale_match_ids)).delete(synchronize_session=False)
        db.query(BankMatchExternal).filter(
            BankMatchExternal.match_id.in_(stale_match_ids)).delete(synchronize_session=False)
        db.query(BankMatch).filter(
            BankMatch.id.in_(stale_match_ids)).delete(synchronize_session=False)
        db.flush()

    removed = len(doomed_line_ids)
    kept = len(attested_line_ids) - removed
    for line_id in doomed_line_ids:
        line = db.get(BankStatementLine, line_id)
        if line is not None:
            db.delete(line)

    imp.voided_at = utcnow()
    db.commit()
    for path in image_paths:
        with contextlib.suppress(OSError):
            Path(path).unlink(missing_ok=True)
    return {"status": "voided", "import_id": str(imp.id),
            "lines_removed": removed, "lines_kept_co_attested": kept}
