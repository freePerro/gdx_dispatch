"""Contract test for the D44 test_residue_sweep detector.

Proves the detector catches its canonical failure class: rows with NULL
tenant attribution on a tenant-scoped table. This is the template for
other detector contracts. One file per detector.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine, text

from gdx_dispatch.tests.contracts.detector_contract import (
    DetectorContract,
    register_detector,
    run_contract,
)
from gdx_dispatch.tools.test_residue_sweep import (
    RESIDUE_PATTERNS,
    classify_table,
)


def _seed_orphan_row(engine) -> None:
    """Inject a row with NULL company_id. The detector MUST catch this."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users_contract_test (
                id INTEGER PRIMARY KEY,
                email TEXT,
                company_id TEXT
            )
        """))
        conn.execute(text(
            "INSERT INTO users_contract_test (id, email, company_id) "
            "VALUES (1, 'orphan@example.com', NULL), "
            "       (2, 'scoped@x.com', 'gdx')"
        ))


def _run_detector(engine):
    """Register the test table as if it had residue patterns, run classify."""
    if "users_contract_test" not in RESIDUE_PATTERNS:
        RESIDUE_PATTERNS["users_contract_test"] = [
            ("email", r"@example\.(com|org)$"),
        ]
    raw = engine.raw_connection()
    try:
        raw.create_function(
            "REGEXP", 2,
            lambda p, v: v is not None and re.search(p, v) is not None,
        )
        return classify_table(
            engine, text, "users_contract_test", {"company_id"},
            tenant_slug="contract_tenant",
        )
    finally:
        raw.close()


def _assert_detector_fires(result) -> None:
    """The detector must classify the orphan row as residue (email matches
    @example.com) AND report it in the returned finding."""
    assert result is not None, "detector returned None for unhealthy state"
    assert result.deletable_residue_count == 1, (
        f"expected 1 residue row matching @example.com, got {result}"
    )


CONTRACT = DetectorContract(
    name="test_residue_sweep",
    description=(
        "D44 test_residue_sweep must classify a NULL-company_id row "
        "matching @example.com as deletable residue"
    ),
    seed=_seed_orphan_row,
    run=_run_detector,
    assert_fires=_assert_detector_fires,
)

register_detector(CONTRACT)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    yield eng
    eng.dispose()


def test_test_residue_sweep_contract(engine):
    """The contract: seed unhealthy, run detector, assert it fires."""
    run_contract(CONTRACT, engine)
