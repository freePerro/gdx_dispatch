from types import SimpleNamespace

import pytest
from sqlalchemy import select

from gdx_dispatch.core.custom_fields import validate_custom_fields
from gdx_dispatch.core.gdpr import delete_customer_data, export_customer_data
from gdx_dispatch.models.tenant_models import Customer


def test_gdpr_export_customer(tenant_db):
    c = Customer(name="Jane Doe", email="jane@example.com", company_id="tenant-test"); tenant_db.add(c); tenant_db.commit(); tenant_db.refresh(c)  # noqa: E701,E702
    out = export_customer_data(str(c.id), tenant_db)
    assert "customer" in out and out["customer"]["name"] == "Jane Doe"


def test_gdpr_soft_delete(tenant_db):
    c = Customer(name="Soft Delete", company_id="tenant-test"); tenant_db.add(c); tenant_db.commit(); tenant_db.refresh(c)  # noqa: E701,E702
    delete_customer_data(str(c.id), tenant_db, hard=False); tenant_db.refresh(c)  # noqa: E701,E702
    assert c.deleted_at is not None


def test_gdpr_hard_delete(tenant_db):
    c = Customer(name="Hard Delete", email="hard@example.com", company_id="tenant-test"); tenant_db.add(c); tenant_db.commit(); tenant_db.refresh(c)  # noqa: E701,E702
    delete_customer_data(str(c.id), tenant_db, hard=False); delete_customer_data(str(c.id), tenant_db, hard=True)  # noqa: E701,E702
    c2 = tenant_db.execute(select(Customer).where(Customer.id == c.id)).scalar_one()
    assert c2.name == "[DELETED]" and c2.email is None


def test_custom_fields_cx_prefix_validation():
    d = SimpleNamespace(field_key="cx_color", required=False, field_type="text")
    db = SimpleNamespace(execute=lambda *_a, **_k: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [d])))
    assert validate_custom_fields({"cx_color": "red"}, "job", db) == {"cx_color": "red"}
    with pytest.raises(ValueError):
        validate_custom_fields({"bad_key": "value"}, "job", db)


# test_magic_link_verify removed per ADR-018: it exercised the deleted
# cookie-flow helpers. The live equivalents are covered in
# test_customer_portal.py (verify round-trip, single-use, invite flow).
