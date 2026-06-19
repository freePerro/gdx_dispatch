from uuid import uuid4
from gdx_dispatch.core.unified_principal import Principal


def test_principal_ai_worker_construction():
    identity_id = uuid4()
    tenant_id = "tenant-123"
    role = "ai_worker"
    caps = (("read", "customer"),)
    admin_uuid = str(uuid4())

    p = Principal(
        identity_id=identity_id,
        tenant_id=tenant_id,
        principal_role=role,
        capabilities=caps,
        auth_kind="session",
        session_id="sess-123",
        actor_type="ai_worker",
        delegated_by_user_id=admin_uuid,
    )

    assert p.actor_type == "ai_worker"
    assert p.delegated_by_user_id == admin_uuid
    assert p.identity_id == identity_id
    assert p.capabilities == (("read", "customer"),)


def test_principal_default_actor_type():
    identity_id = uuid4()
    tenant_id = "tenant-123"
    role = "user"
    caps = (("read", "customer"),)

    p = Principal(
        identity_id=identity_id,
        tenant_id=tenant_id,
        principal_role=role,
        capabilities=caps,
        auth_kind="session",
        session_id="sess-123",
    )

    assert p.actor_type == "human"
    assert p.delegated_by_user_id is None
