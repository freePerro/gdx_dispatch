"""SS-34 slice E tests — dr_drill_cron CLI."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from gdx_dispatch.core.dr.drill_orchestrator import DrillReport, reset_idempotency_cache
from gdx_dispatch.core.dr.restore_to_staging import ProductionTargetRefused
from gdx_dispatch.tools import dr_drill_cron


@pytest.fixture(autouse=True)
def _clear():
    reset_idempotency_cache()
    yield
    reset_idempotency_cache()


def _passing_report(**_kw) -> DrillReport:
    return DrillReport(
        drill_run_id=str(uuid4()),
        scheduled_for=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        passed=True,
    )


def _failing_report(**_kw) -> DrillReport:
    return DrillReport(
        drill_run_id=str(uuid4()),
        scheduled_for=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        passed=False,
        failure_reason="verification: 1 failed checks",
    )


def test_cli_scope_tenant_requires_selector(capsys):
    with pytest.raises(SystemExit):
        dr_drill_cron.main([
            "--scope=tenant",
            "--source-db=postgresql://u@src/d",
            "--staging-db=postgresql://u@stg/d",
            "--snapshot-target=/tmp/s.pgc",
        ])


def test_cli_happy_path_exits_zero():
    with patch(
        "gdx_dispatch.tools.dr_drill_cron.run_drill",
        side_effect=_passing_report,
    ):
        rc = dr_drill_cron.main([
            "--scope=full",
            "--source-db=postgresql://u@src/d",
            "--staging-db=postgresql://u@stg/d",
            "--snapshot-target=/tmp/s.pgc",
        ])
    assert rc == 0


def test_cli_verification_failure_exits_one():
    with patch(
        "gdx_dispatch.tools.dr_drill_cron.run_drill",
        side_effect=_failing_report,
    ):
        rc = dr_drill_cron.main([
            "--scope=full",
            "--source-db=postgresql://u@src/d",
            "--staging-db=postgresql://u@stg/d",
            "--snapshot-target=/tmp/s.pgc",
        ])
    assert rc == 1


def test_cli_production_refused_exits_three():
    with patch(
        "gdx_dispatch.tools.dr_drill_cron.run_drill",
        side_effect=ProductionTargetRefused("no."),
    ):
        rc = dr_drill_cron.main([
            "--scope=full",
            "--source-db=postgresql://u@src/d",
            "--staging-db=postgresql://u@stg/d",
            "--snapshot-target=/tmp/s.pgc",
        ])
    assert rc == 3


def test_cli_infra_failure_exits_two():
    with patch(
        "gdx_dispatch.tools.dr_drill_cron.run_drill",
        side_effect=RuntimeError("pg_dump missing"),
    ):
        rc = dr_drill_cron.main([
            "--scope=full",
            "--source-db=postgresql://u@src/d",
            "--staging-db=postgresql://u@stg/d",
            "--snapshot-target=/tmp/s.pgc",
        ])
    assert rc == 2


def test_cli_json_output(capsys):
    with patch(
        "gdx_dispatch.tools.dr_drill_cron.run_drill",
        side_effect=_passing_report,
    ):
        dr_drill_cron.main([
            "--scope=full",
            "--source-db=postgresql://u@src/d",
            "--staging-db=postgresql://u@stg/d",
            "--snapshot-target=/tmp/s.pgc",
            "--json",
        ])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["passed"] is True
