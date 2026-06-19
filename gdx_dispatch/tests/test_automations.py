from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase


# Lightweight Request shim for calling the route functions directly. The
# automation handlers now take `request: Request` so they can feed the audit
# logger; tests don't hit the FastAPI app stack, so we supply a stub with
# the minimal shape the handlers touch (state.tenant + client.host).
def _mock_request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": "test-tenant"}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )
from gdx_dispatch.models.tenant_models import AutomationEnrollment, AutomationSequence, AutomationStep
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.automations import (
    SequenceCreateIn,
    SequencePatchIn,
    StepCreateIn,
    add_step,
    create_automation,
    delete_automation,
    get_automation,
    list_automations,
    list_enrollments,
    patch_automation,
    pause_automation,
    resume_automation,
    router,
)


@pytest.fixture()
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _create_sequence(db, **overrides) -> dict:
    payload = SequenceCreateIn(
        name="Post Job Follow-up",
        trigger_event="job_completed",
        steps=[
            {
                "action_type": "send_email",
                "delay_hours": 2,
                "template": "Thanks for choosing us",
            }
        ],
    )
    data = payload.model_dump()
    data.update(overrides)
    return create_automation(
        payload=SequenceCreateIn(**data),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=db,
    )


def _seed_enrollment(db, sequence_id: str, status: str = "active") -> str:
    enrollment = AutomationEnrollment(
        sequence_id=UUID(sequence_id),
        entity_type="job",
        entity_id=str(uuid4()),
        status=status,
    )
    db.add(enrollment)
    db.commit()
    db.refresh(enrollment)
    return str(enrollment.id)


def test_all_automation_routes_require_auth_dependency():
    guarded_paths = set()
    for route in router.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is get_current_user:
                guarded_paths.add(route.path)
                break

    assert "/api/automations" in guarded_paths
    assert "/api/automations/{automation_id}" in guarded_paths
    assert "/api/automations/{automation_id}/steps" in guarded_paths
    assert "/api/automations/{automation_id}/enrollments" in guarded_paths


def test_create_automation_sequence_with_steps(tenant_db_session):
    data = _create_sequence(tenant_db_session)

    assert data["name"] == "Post Job Follow-up"
    assert data["trigger_event"] == "job_completed"
    assert data["is_active"] is True
    assert data["is_paused"] is False
    assert len(data["steps"]) == 1
    assert data["steps"][0]["action_type"] == "send_email"
    assert data["steps"][0]["delay_hours"] == 2


def test_create_automation_rejects_invalid_trigger_event():
    with pytest.raises(Exception):
        SequenceCreateIn(name="Invalid", trigger_event="not_real", steps=[])


def test_list_automations_excludes_soft_deleted(tenant_db_session):
    keep = _create_sequence(tenant_db_session, name="Keep Me")
    remove = _create_sequence(tenant_db_session, name="Delete Me")

    del_result = delete_automation(
        automation_id=UUID(remove["id"]),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=tenant_db_session,
    )
    assert del_result["deleted"] is True

    items = list_automations(_={}, db=tenant_db_session)
    ids = {item["id"] for item in items}
    assert keep["id"] in ids
    assert remove["id"] not in ids


def test_get_automation_by_id_includes_steps(tenant_db_session):
    created = _create_sequence(
        tenant_db_session,
        steps=[
            {"action_type": "send_email", "delay_hours": 1, "template": "email-1"},
            {"action_type": "wait", "delay_hours": 24, "template": ""},
        ],
    )

    data = get_automation(automation_id=UUID(created["id"]), _={}, db=tenant_db_session)
    assert data["id"] == created["id"]
    assert len(data["steps"]) == 2
    assert [s["step_order"] for s in data["steps"]] == [1, 2]


def test_patch_automation_updates_name_and_trigger(tenant_db_session):
    created = _create_sequence(tenant_db_session)

    data = patch_automation(
        automation_id=UUID(created["id"]),
        payload=SequencePatchIn(name="Updated Name", trigger_event="estimate_sent"),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=tenant_db_session,
    )
    assert data["name"] == "Updated Name"
    assert data["trigger_event"] == "estimate_sent"


def test_patch_automation_404_for_missing_sequence(tenant_db_session):
    with pytest.raises(Exception) as exc:
        patch_automation(
            automation_id=uuid4(),
            payload=SequencePatchIn(name="Nope"),
            request=_mock_request(),
            user={"sub": "test-user"},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 404


def test_add_step_to_existing_sequence(tenant_db_session):
    created = _create_sequence(tenant_db_session, steps=[])

    step = add_step(
        automation_id=UUID(created["id"]),
        payload=StepCreateIn(action_type="send_sms", delay_hours=6, template="Reminder text"),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=tenant_db_session,
    )
    assert step["action_type"] == "send_sms"
    assert step["step_order"] == 1


def test_add_step_rejects_invalid_action_type():
    with pytest.raises(Exception):
        StepCreateIn(action_type="bad_action", delay_hours=0, template="n/a")


def test_list_active_enrollments_for_sequence(tenant_db_session):
    created = _create_sequence(tenant_db_session)
    active_id = _seed_enrollment(tenant_db_session, created["id"], status="active")
    _seed_enrollment(tenant_db_session, created["id"], status="completed")

    items = list_enrollments(automation_id=UUID(created["id"]), _={}, db=tenant_db_session)
    assert len(items) == 1
    assert items[0]["id"] == active_id
    assert items[0]["status"] == "active"


def test_pause_and_resume_sequence(tenant_db_session):
    created = _create_sequence(tenant_db_session)

    paused = pause_automation(
        automation_id=UUID(created["id"]),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=tenant_db_session,
    )
    assert paused["is_paused"] is True

    resumed = resume_automation(
        automation_id=UUID(created["id"]),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=tenant_db_session,
    )
    assert resumed["is_paused"] is False


def test_delete_automation_soft_delete_sets_deleted_at(tenant_db_session):
    created = _create_sequence(tenant_db_session)

    out = delete_automation(
        automation_id=UUID(created["id"]),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=tenant_db_session,
    )
    assert out["deleted"] is True

    row = tenant_db_session.get(AutomationSequence, UUID(created["id"]))
    assert row is not None
    assert row.deleted_at is not None


def test_get_automation_404_when_deleted(tenant_db_session):
    created = _create_sequence(tenant_db_session)
    delete_automation(
        automation_id=UUID(created["id"]),
        request=_mock_request(),
        user={"sub": "test-user"},
        db=tenant_db_session,
    )

    with pytest.raises(Exception) as exc:
        get_automation(automation_id=UUID(created["id"]), _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


def test_create_sequence_persists_step_rows(tenant_db_session):
    created = _create_sequence(
        tenant_db_session,
        steps=[
            {"action_type": "send_email", "delay_hours": 0, "template": "A"},
            {"action_type": "create_task", "delay_hours": 3, "template": "B"},
        ],
    )

    steps = (
        tenant_db_session.query(AutomationStep)
        .filter(AutomationStep.sequence_id == UUID(created["id"]))
        .order_by(AutomationStep.step_order.asc())
        .all()
    )
    assert len(steps) == 2
    assert steps[0].step_order == 1
    assert steps[1].step_order == 2
