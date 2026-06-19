from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.custom_fields import validate_custom_fields
from gdx_dispatch.core.gdpr import delete_customer_data, export_customer_data
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.modules.customer_portal.auth import send_magic_link, verify_magic_link
from gdx_dispatch.modules.customer_portal.models import CustomerUser
from gdx_dispatch.modules.fleet.models import Vehicle
from gdx_dispatch.modules.fleet.service import get_due_maintenance, log_service
from gdx_dispatch.modules.inventory.aliases import create_alias, resolve_local_sku, resolve_upstream_sku


@pytest.fixture(autouse=True)
def _mock_stripe(monkeypatch):
    monkeypatch.setattr("gdx_dispatch.core.reconciliation.stripe.Subscription.retrieve", lambda *_a, **_k: {"items": {"data": []}})


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


def test_part_alias_roundtrip(control_db):
    a = Tenant(slug="tenant-a", name="Tenant A"); b = Tenant(slug="tenant-b", name="Tenant B")  # noqa: E701,E702
    control_db.add_all([a, b]); control_db.commit(); control_db.refresh(a); control_db.refresh(b)  # noqa: E701,E702
    create_alias(a.id, b.id, "LOCAL-SKU-1", "SUPPLIER-SKU-100", control_db); control_db.commit()  # noqa: E701,E702
    assert resolve_upstream_sku(a.id, b.id, "LOCAL-SKU-1", control_db) == "SUPPLIER-SKU-100"
    assert resolve_local_sku(a.id, b.id, "SUPPLIER-SKU-100", control_db) == "LOCAL-SKU-1"


def test_fleet_service_log(tenant_db):
    v = Vehicle(make="Ford", model="Transit", year=2022); tenant_db.add(v); tenant_db.commit(); tenant_db.refresh(v)  # noqa: E701,E702
    log_service(v.id, "oil_change", 15000, datetime.now(UTC), 50.0, "Synthetic", tenant_db); tenant_db.refresh(v)  # noqa: E701,E702
    assert v.last_service_odometer == 15000


def test_fleet_due_maintenance(tenant_db):
    v = Vehicle(make="Chevy", model="Express", year=2020, odometer=20000, last_service_odometer=10000, service_interval_miles=5000)
    tenant_db.add(v); tenant_db.commit(); tenant_db.refresh(v)  # noqa: E701,E702
    assert v.id in [row.id for row in get_due_maintenance(tenant_db)]


def test_custom_fields_cx_prefix_validation():
    d = SimpleNamespace(field_key="cx_color", required=False, field_type="text")
    db = SimpleNamespace(execute=lambda *_a, **_k: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [d])))
    assert validate_custom_fields({"cx_color": "red"}, "job", db) == {"cx_color": "red"}
    with pytest.raises(ValueError):
        validate_custom_fields({"bad_key": "value"}, "job", db)


def test_magic_link_verify(tenant_db):
    c = Customer(name="Portal User", email="portal@example.com", company_id="tenant-test"); tenant_db.add(c); tenant_db.commit(); tenant_db.refresh(c)  # noqa: E701,E702
    u = CustomerUser(customer_id=c.id, email="portal@example.com", is_active=True); tenant_db.add(u); tenant_db.commit()  # noqa: E701,E702
    url = send_magic_link("portal@example.com", c.id, tenant_db); token = url.rsplit("/", 1)[-1]  # noqa: E701,E702
    assert verify_magic_link(token, tenant_db).email == "portal@example.com"
    assert verify_magic_link(token, tenant_db) is None
