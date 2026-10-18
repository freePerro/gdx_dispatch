"""Every users.role writer rewrites the user's UserRoleAssignment (GDXA-1453).

The permission resolver (core/modules.py::_load_user_permissions step 4)
trusts a lone assignment snapshot over users.role, on REST and the board
socket alike. A writer that changes users.role and leaves the old row behind
therefore keeps the old role's permissions. admin_ops.patch_user is covered in
test_admin_ops.py; these cover the two boot-time writers.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from gdx_dispatch.models.tenant_models import TenantRole, User, UserRoleAssignment
from gdx_dispatch.tests.conftest import make_fresh_db, production_sessionmaker

SEED_PATH = Path(__file__).resolve().parents[1] / "docker" / "demo" / "seed_demo.py"
TENANT = "00000000-0000-0000-0000-0000000000d1"


def _role(db, name: str, perms: list[str]) -> TenantRole:
    role = TenantRole(id=uuid4(), company_id=TENANT, name=name, permissions=json.dumps(perms), is_system=True)
    db.add(role)
    db.flush()
    return role


def _assigned_role_names(db, user_id: str) -> list[str]:
    return list(db.execute(
        select(TenantRole.name)
        .join(UserRoleAssignment, UserRoleAssignment.role_id == TenantRole.id)
        .where(UserRoleAssignment.user_id == user_id)
    ).scalars())


def test_demo_seed_refresh_drops_a_promoted_logins_assignment(monkeypatch):
    spec = importlib.util.spec_from_file_location("seed_demo_role_sync", SEED_PATH)
    seed_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_demo)

    engine = make_fresh_db()
    Session = production_sessionmaker(engine)
    monkeypatch.setattr(seed_demo, "SessionLocal", Session)
    monkeypatch.setattr(seed_demo, "company_id", lambda: TENANT)
    monkeypatch.setattr(seed_demo, "allocate_job_number", lambda _t, customer_name=None: f"J-{uuid4().hex[:6]}")

    email = next(e for e, role, *_ in seed_demo.ROLE_USERS if role == "technician")
    db = Session()
    try:
        admin = _role(db, "admin", ["jobs.read_all", "users.write"])
        tech = _role(db, "technician", ["jobs.read_own"])
        # The demo technician login, promoted to admin in the UI since the last
        # seed: users.role and the assignment both say admin.
        user = User(company_id=TENANT, email=email, username=email, role="admin",
                    password_hash=generate_password_hash("x"))
        db.add(user)
        db.flush()
        user_id = str(user.id)
        db.add(UserRoleAssignment(id=uuid4(), company_id=TENANT, user_id=user_id, role_id=admin.id))
        db.commit()

        seed_demo.seed()

        db.expire_all()
        assert db.get(User, user.id).role == "technician"
        assert _assigned_role_names(db, user_id) == [tech.name]
    finally:
        db.close()
        engine.dispose()


def test_bootstrap_owner_restore_drops_the_tombstones_assignment():
    from gdx_dispatch.core.audit import utcnow
    from gdx_dispatch.tools.bootstrap_app import _revive_owner

    engine = make_fresh_db()
    db = production_sessionmaker(engine)()
    try:
        tech = _role(db, "technician", ["jobs.read_own"])
        owner = _role(db, "owner", ["*"])
        user = User(company_id=TENANT, email="owner@example.com", username="admin", role="technician",
                    password_hash="x", deleted_at=utcnow())
        db.add(user)
        db.flush()
        user_id = str(user.id)
        db.add(UserRoleAssignment(id=uuid4(), company_id=TENANT, user_id=user_id, role_id=tech.id))
        db.commit()

        _revive_owner(db, TENANT, user, "new-hash")
        db.commit()

        db.expire_all()
        restored = db.get(User, user.id)
        assert restored.role == "owner" and restored.deleted_at is None
        assert _assigned_role_names(db, user_id) == [owner.name]
    finally:
        db.close()
        engine.dispose()
