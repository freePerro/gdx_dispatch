"""SS-34 slice C tests — verification_harness.

The harness is pure-functional against a ``db_exec`` callable, so we
inject a stub ``FakeExec`` with configurable responses.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from gdx_dispatch.core.dr.verification_harness import (
    CheckResult,
    VerificationConfig,
    VerificationReport,
    run_verification,
)


class FakeExec:
    """Dispatches SQL strings to canned responses by keyword match.

    Pass a dict like ``{"identities": [(42,)]}``. Any SQL that doesn't
    match a keyword returns an empty list.
    """

    def __init__(self, responses=None, raise_on=None):
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.calls: list[str] = []

    def __call__(self, sql: str):
        self.calls.append(sql)
        for keyword, exc in self.raise_on.items():
            if keyword in sql:
                raise exc
        for keyword, rows in self.responses.items():
            if keyword in sql:
                return rows
        return []


def test_row_count_in_range_passes():
    exec_ = FakeExec(responses={
        '"identities"': [(42,)],
        '"tenants"': [(3,)],
        '"customers"': [(100,)],
        '"jobs"': [(200,)],
        '"audit_logs"': [(5_000,)],
        "pg_policies": [(1,)],
        "tenants WHERE slug": [(1,)],
    })
    cfg = VerificationConfig(tenant_ids_to_verify=())
    r = run_verification(db_exec=exec_, config=cfg)
    # All row counts in range, RLS present, system tenant present.
    assert r.passed, r.to_dict()


def test_row_count_out_of_range_fails_but_does_not_raise():
    exec_ = FakeExec(responses={
        '"identities"': [(0,)],  # below lo=1
        "pg_policies": [(1,)],
        "tenants WHERE slug": [(1,)],
    })
    r = run_verification(db_exec=exec_, config=VerificationConfig())
    assert not r.passed
    names = [c.name for c in r.failed_checks]
    assert "rowcount:identities" in names
    # Other row-count checks will also fail because FakeExec returns
    # [] → count=0, but lo=1 for identities/tenants so those fail too.
    # Check that the specific out-of-range message is present.
    bad = next(c for c in r.checks if c.name == "rowcount:identities")
    assert "OUT OF RANGE" in bad.detail


def test_query_exception_is_captured_not_raised():
    exec_ = FakeExec(
        responses={"pg_policies": [(1,)], "tenants WHERE slug": [(1,)]},
        raise_on={'"identities"': RuntimeError("connection closed")},
    )
    r = run_verification(db_exec=exec_, config=VerificationConfig())
    failed = {c.name: c for c in r.failed_checks}
    assert "rowcount:identities" in failed
    assert "connection closed" in failed["rowcount:identities"].detail


def test_missing_rls_policy_fails():
    exec_ = FakeExec(responses={
        '"identities"': [(1,)],
        '"tenants"': [(1,)],
        '"customers"': [(1,)],
        '"jobs"': [(1,)],
        '"audit_logs"': [(1,)],
        "pg_policies": [(0,)],  # no policies
        "tenants WHERE slug": [(1,)],
    })
    r = run_verification(db_exec=exec_, config=VerificationConfig())
    assert not r.passed
    names = [c.name for c in r.failed_checks]
    # Three-plane model (an earlier session, migration A): RLS is required on
    # control + commerce plane tables only. Tenant-plane (customers, jobs,
    # audit_logs) isolation is per-DB connection, not RLS.
    assert "rls:cross_tenant_share" in names
    assert "rls:audit_retention_policy" in names


def test_missing_system_tenant_fails():
    exec_ = FakeExec(responses={
        '"identities"': [(1,)],
        '"tenants"': [(1,)],
        '"customers"': [(1,)],
        '"jobs"': [(1,)],
        '"audit_logs"': [(1,)],
        "pg_policies": [(1,)],
        "tenants WHERE slug": [(0,)],
    })
    r = run_verification(db_exec=exec_, config=VerificationConfig())
    failed = {c.name for c in r.failed_checks}
    assert "critical:system_tenant" in failed


def test_known_identity_check():
    exec_ = FakeExec(responses={
        '"identities"': [(1,)],
        '"tenants"': [(1,)],
        '"customers"': [(1,)],
        '"jobs"': [(1,)],
        '"audit_logs"': [(1,)],
        "pg_policies": [(1,)],
        "tenants WHERE slug": [(1,)],
        "identities WHERE id = 'known-i-1'": [(1,)],
    })
    cfg = VerificationConfig(known_identity_id="known-i-1")
    r = run_verification(db_exec=exec_, config=cfg)
    assert r.passed, r.to_dict()
    assert any(c.name == "critical:identity:known-i-1" for c in r.checks)


def test_hash_chain_skipped_when_no_db_session():
    exec_ = FakeExec(responses={
        '"identities"': [(1,)],
        '"tenants"': [(1,)],
        '"customers"': [(1,)],
        '"jobs"': [(1,)],
        '"audit_logs"': [(1,)],
        "pg_policies": [(1,)],
        "tenants WHERE slug": [(1,)],
    })
    cfg = VerificationConfig(tenant_ids_to_verify=("t1",))
    r = run_verification(db_exec=exec_, config=cfg)
    # Skipped is counted as failed so the drill sees the gap.
    assert not r.passed
    assert any(c.name == "hashchain:skipped" for c in r.failed_checks)


def test_hash_chain_intact_passes():
    exec_ = FakeExec(responses={
        '"identities"': [(1,)],
        '"tenants"': [(1,)],
        '"customers"': [(1,)],
        '"jobs"': [(1,)],
        '"audit_logs"': [(1,)],
        "pg_policies": [(1,)],
        "tenants WHERE slug": [(1,)],
    })
    cfg = VerificationConfig(tenant_ids_to_verify=("tenant-a",))
    with patch(
        "gdx_dispatch.core.audit_hash_chain.verify_chain",
        return_value=(True, -1),
    ):
        r = run_verification(
            db_exec=exec_,
            db_for_hashchain=object(),
            config=cfg,
        )
    assert r.passed, r.to_dict()


def test_hash_chain_broken_fails_with_index():
    exec_ = FakeExec(responses={
        '"identities"': [(1,)],
        '"tenants"': [(1,)],
        '"customers"': [(1,)],
        '"jobs"': [(1,)],
        '"audit_logs"': [(1,)],
        "pg_policies": [(1,)],
        "tenants WHERE slug": [(1,)],
    })
    cfg = VerificationConfig(tenant_ids_to_verify=("tenant-b",))
    with patch(
        "gdx_dispatch.core.audit_hash_chain.verify_chain",
        return_value=(False, 7),
    ):
        r = run_verification(
            db_exec=exec_,
            db_for_hashchain=object(),
            config=cfg,
        )
    failed = {c.name: c for c in r.failed_checks}
    assert "hashchain:tenant-b" in failed
    assert "index 7" in failed["hashchain:tenant-b"].detail


def test_report_to_dict_shape():
    exec_ = FakeExec()
    r = run_verification(db_exec=exec_, config=VerificationConfig())
    d = r.to_dict()
    assert "run_started_at" in d
    assert "run_finished_at" in d
    assert "passed" in d
    assert "failed_count" in d
    assert isinstance(d["checks"], list)
    # Every check dict has name/passed/detail
    for c in d["checks"]:
        assert set(c.keys()) == {"name", "passed", "detail"}
