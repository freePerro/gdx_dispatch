"""bootstrap_app step 5: the default customer-alert tags are seeded once, on
the install's first boot, with an audit row — never re-seeded after an owner
empties the list (the entrypoint runs bootstrap on every boot)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.models.tenant_models import Base, CompanyModuleGrant, Tag
from gdx_dispatch.tools.bootstrap_app import seed_customer_alert_tags_on_first_boot

TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine, tables=[Tag.__table__, CompanyModuleGrant.__table__])
    AuditLog.__table__.create(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        yield s
    engine.dispose()


def _audit_actions(db) -> list[str]:
    return [a for (a,) in db.query(AuditLog.action).all()]


def test_first_boot_seeds_and_audits(db):
    assert seed_customer_alert_tags_on_first_boot(db, TENANT) > 0
    assert db.query(Tag).count() > 0
    assert all(t.company_id == TENANT for t in db.query(Tag).all())
    assert "customer_alert_tags_seeded" in _audit_actions(db)


def test_second_boot_is_a_no_op(db):
    seed_customer_alert_tags_on_first_boot(db, TENANT)
    n = db.query(Tag).count()
    assert seed_customer_alert_tags_on_first_boot(db, TENANT) == 0
    assert db.query(Tag).count() == n
    assert _audit_actions(db).count("customer_alert_tags_seeded") == 1


def test_an_emptied_list_on_a_booted_install_stays_empty(db):
    """The owner deleted every tag on purpose; a restart must not bring the
    fourteen defaults back."""
    db.add(CompanyModuleGrant(id=str(uuid4()), company_id=TENANT, module_key="jobs"))
    db.commit()
    assert db.query(Tag).count() == 0
    assert seed_customer_alert_tags_on_first_boot(db, TENANT) == 0
    assert db.query(Tag).count() == 0
    assert _audit_actions(db) == []
