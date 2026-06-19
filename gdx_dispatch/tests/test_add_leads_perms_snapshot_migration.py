"""D-leads-authz-sweep — tests for the additive leads.* snapshot migration.

Pins the safety properties that make Approach D correct: ADD-only,
matrix-correct, is_system+name gated (custom roles untouched), the
non-matrix builtin roles untouched, idempotent, dry-run writes nothing.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.permissions import BUILTIN_ROLES
from gdx_dispatch.models.tenant_models import TenantRole
from gdx_dispatch.tools.add_leads_perms_to_role_snapshots import LEADS_ROLE_GRANTS, amend_one


def test_grants_in_exact_lockstep_with_builtin_roles():
    """Drift guard: the migration's per-role leads.* grants MUST equal
    the leads.* keys BUILTIN_ROLES gives that role. If permissions.py
    adds/changes a role's leads.* and this dict isn't updated (or vice
    versa), a role gets silently gated out (snapshot lacks a key the
    route requires) or over-granted. Fail loudly here, not in prod."""
    for role, keys in LEADS_ROLE_GRANTS.items():
        builtin_leads = {k for k in BUILTIN_ROLES.get(role, []) if k.startswith("leads.")}
        assert set(keys) == builtin_leads, (
            f"{role}: migration grants {sorted(keys)} but BUILTIN_ROLES "
            f"has {sorted(builtin_leads)} — lockstep broken"
        )
    # Roles NOT in the migration dict must also have NO leads.* in BUILTIN
    # (except admin/owner who resolve via live BUILTIN, step 3, no snapshot
    # edit needed; technician must have none).
    for role in ("technician",):
        assert not {k for k in BUILTIN_ROLES.get(role, []) if k.startswith("leads.")}, (
            f"{role} unexpectedly has leads.* in BUILTIN but is absent from the migration"
        )

CO = "tenant-mig"


@pytest.fixture()
def db_url(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    eng = create_engine(url, future=True)
    TenantBase.metadata.create_all(eng, checkfirst=True)
    S = sessionmaker(bind=eng, future=True)
    # tenant_roles has a UNIQUE (company_id, name) constraint, so a name
    # is EITHER the seeded builtin (is_system=True) OR a tenant repurpose
    # (is_system=False) — never both. The tool's is_system gate is what
    # distinguishes them.
    with S() as db:
        def role(name, perms, is_system=True):
            return TenantRole(id=uuid4(), company_id=CO, name=name,
                               permissions=json.dumps(perms), is_system=is_system)
        db.add_all([
            role("sales", ["customers.read_all", "customers.write"]),   # stale builtin → r/w/d
            role("dispatcher", ["jobs.read_all"]),                       # stale builtin → r/w/d
            role("viewer", ["jobs.read_all"]),                           # stale builtin → read
            role("accounting", ["invoices.read_all"], is_system=False),  # REPURPOSED matrix name → skip (is_system gate)
            role("technician", ["jobs.read_own"]),                       # builtin, NOT in matrix → untouched
            role("admin", ["*"]),                                        # builtin, NOT in matrix → untouched
            role("sales-helper", ["estimates.read_all"], is_system=False),  # custom non-matrix name → untouched
        ])
        db.commit()
    eng.dispose()
    return url


def _perms(url, name, is_system):
    eng = create_engine(url, future=True)
    with Session(eng, future=True) as db:
        rows = db.execute(select(TenantRole).where(
            TenantRole.name == name, TenantRole.is_system.is_(is_system))).scalars().all()
        eng.dispose()
        return [json.loads(r.permissions) for r in rows]


def test_additive_and_matrix_correct(db_url):
    c = amend_one("t", db_url, dry_run=False)
    sales = set(_perms(db_url, "sales", True)[0])
    assert {"customers.read_all", "customers.write"} <= sales            # existing preserved
    assert {"leads.read", "leads.write", "leads.delete"} <= sales        # full matrix
    dispatcher = set(_perms(db_url, "dispatcher", True)[0])
    assert {"leads.read", "leads.write", "leads.delete"} <= dispatcher
    viewer = _perms(db_url, "viewer", True)[0]
    assert "leads.read" in viewer and "leads.write" not in viewer        # read-only
    assert viewer.count("leads.read") == 1                               # no dup
    # sales(3) + dispatcher(3) + viewer(1); accounting skipped (is_system=False)
    assert c["keys_added"] == 7
    assert c["roles_amended"] == 3


def test_non_matrix_builtin_roles_untouched(db_url):
    amend_one("t", db_url, dry_run=False)
    assert _perms(db_url, "technician", True)[0] == ["jobs.read_own"]
    assert _perms(db_url, "admin", True)[0] == ["*"]


def test_is_system_gate_skips_repurposed_matrix_name(db_url):
    """A tenant repurpose of a matrix name (is_system=False 'accounting')
    must be skipped — the tool only amends the SEEDED builtin row."""
    c = amend_one("t", db_url, dry_run=False)
    acct = _perms(db_url, "accounting", False)[0]
    assert acct == ["invoices.read_all"], "repurposed (is_system=False) row must be untouched"
    assert c["roles_absent"] >= 1


def test_custom_nonmatrix_role_untouched(db_url):
    amend_one("t", db_url, dry_run=False)
    assert _perms(db_url, "sales-helper", False)[0] == ["estimates.read_all"]


def test_idempotent(db_url):
    amend_one("t", db_url, dry_run=False)
    c2 = amend_one("t", db_url, dry_run=False)
    assert c2["keys_added"] == 0
    assert c2["roles_amended"] == 0
    assert c2["roles_noop"] == 3  # sales/dispatcher/viewer now all no-op


def test_dry_run_writes_nothing(db_url):
    c = amend_one("t", db_url, dry_run=True)
    assert c["keys_added"] == 7
    assert "leads.read" not in _perms(db_url, "sales", True)[0]  # unchanged on disk


def test_no_removals_no_foreign_keys(db_url):
    before = set(_perms(db_url, "dispatcher", True)[0])
    amend_one("t", db_url, dry_run=False)
    after = set(_perms(db_url, "dispatcher", True)[0])
    assert before <= after                       # nothing removed
    assert after - before == {"leads.read", "leads.write", "leads.delete"}  # only leads.* added
