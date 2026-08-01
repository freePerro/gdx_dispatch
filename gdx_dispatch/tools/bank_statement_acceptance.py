"""Reproducible real-corpus acceptance for the bank statement import layer.

Runs every PDF in a directory through the FULL import service — real pypdf
extraction, tie-out gate, overlap integrity, dedup/attestation, period-aware
continuity — against a scratch SQLite DB, and exits non-zero on any failure.

The statements themselves stay OUT of the repo (they carry names, addresses
and account digits — public-repo hygiene); this tool is what makes the
"all statements green" acceptance claim reproducible against a local corpus
(docs/design/bank-statement-import-plan.md §8).

Usage (throwaway container, statements mounted read-only):

    docker run --rm --entrypoint python \
      -e JWT_SECRET="dummysecret-dummysecret-dummysecret" \
      -v "$PWD":/app -w /app \
      -v "/path/to/statements":/statements:ro \
      docker-app gdx_dispatch/tools/bank_statement_acceptance.py /statements
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

# Runnable by file path (python gdx_dispatch/tools/…) — put the repo root on
# sys.path so the package imports resolve regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main(statements_dir: str) -> int:
    os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="bank-stmt-acceptance-"))

    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    import gdx_dispatch.models  # noqa: F401  (register all ORM models)
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.modules.bank_feeds import statement_service
    from gdx_dispatch.modules.bank_feeds.statement_models import (
        BankStatementImport,
        BankStatementLine,
        BankStatementLineSource,
    )

    engine = create_engine("sqlite://")
    TenantBase.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    corpus = sorted(Path(statements_dir).glob("*.pdf"))
    if not corpus:
        print(f"no PDFs found in {statements_dir}")
        return 2

    failures = 0
    for pdf in corpus:
        pdf_bytes = pdf.read_bytes()
        result = statement_service.import_statement(db, pdf_bytes, pdf.name, imported_by="acceptance")
        status = result["status"]
        ok = status == "imported"
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL':4} {pdf.name[:52]:52} -> {status}"
              f"  +{result.get('lines_added', 0)} lines"
              f" / {result.get('lines_deduped', 0)} deduped"
              f"  imgs {result.get('images_paired', 0)}/{result.get('images_extracted', 0)} paired")
        for warning in result.get("continuity_warnings", []):
            failures += 1
            print(f"       CONTINUITY WARNING: {warning}")
        if status == "failed_tie_out":
            sha = hashlib.sha256(pdf_bytes).hexdigest()
            imp = db.query(BankStatementImport).filter_by(file_sha256=sha).first()
            for check in (imp.tie_out_report or {}).get("checks", []):
                if not check["ok"]:
                    print(f"       FAILED CHECK {check['name']}: "
                          f"expected={check['expected']} actual={str(check['actual'])[:200]}")

    co_attested = (
        db.query(BankStatementLineSource.line_id)
        .group_by(BankStatementLineSource.line_id)
        .having(func.count() > 1)
        .count()
    )
    print()
    print(f"imports: {db.query(BankStatementImport).count()}"
          f"  lines: {db.query(BankStatementLine).count()}"
          f"  attestations: {db.query(BankStatementLineSource).count()}"
          f"  co-attested (overlap dedup): {co_attested}")
    print("ALL GREEN" if failures == 0 else f"{failures} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
