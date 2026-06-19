"""Tests for the custom fields router (gdx_dispatch/routers/custom_fields.py)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.routers import custom_fields as cf
from gdx_dispatch.routers.custom_fields import (
    CustomFieldDefinition,
    CustomFieldDefinitionIn,
    CustomFieldValue,
    CustomFieldValueUpsert,
)
from gdx_dispatch.tests.conftest import make_fresh_db

_TEST_USER = {"user_id": "user-1", "sub": "user-1", "role": "admin"}


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


@pytest.fixture()
def session_factory():
    engine = make_fresh_db()
    # Ensure our custom field tables exist (they live on TenantBase, which
    # make_fresh_db() creates via TenantBase.metadata.create_all).
    CustomFieldDefinition.__table__.create(bind=engine, checkfirst=True)
    CustomFieldValue.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def test_create_definition(session_factory):
    db = session_factory()
    try:
        payload = CustomFieldDefinitionIn(
            entity_type="job",
            field_key="gate_code",
            label="Gate Access Code",
            field_type="text",
            required=True,
            sort_order=1,
        )
        result = cf.create_definition(
            payload=payload,
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        assert result["entity_type"] == "job"
        assert result["field_key"] == "gate_code"
        assert result["label"] == "Gate Access Code"
        assert result["field_type"] == "text"
        assert result["required"] is True
        assert result["sort_order"] == 1
        assert "id" in result and result["id"]
    finally:
        db.close()


def test_definitions_tenant_scoped(session_factory):
    db = session_factory()
    try:
        cf.create_definition(
            payload=CustomFieldDefinitionIn(
                entity_type="customer",
                field_key="segment",
                label="Segment",
                field_type="text",
            ),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        cf.create_definition(
            payload=CustomFieldDefinitionIn(
                entity_type="customer",
                field_key="segment",
                label="Segment B",
                field_type="text",
            ),
            request=_request("tenant-b"),
            user=_TEST_USER,
            db=db,
        )

        a_defs = cf.list_definitions(
            request=_request("tenant-a"), _=_TEST_USER, db=db, entity_type="customer"
        )
        b_defs = cf.list_definitions(
            request=_request("tenant-b"), _=_TEST_USER, db=db, entity_type="customer"
        )
        assert len(a_defs) == 1
        assert len(b_defs) == 1
        assert a_defs[0]["label"] == "Segment"
        assert b_defs[0]["label"] == "Segment B"
        # IDs must differ — different rows per tenant
        assert a_defs[0]["id"] != b_defs[0]["id"]
    finally:
        db.close()


def test_field_key_must_be_snake_case():
    # Bad field keys rejected at the Pydantic layer — this is the 422 path.
    with pytest.raises(ValidationError):
        CustomFieldDefinitionIn(
            entity_type="job",
            field_key="BadKey",
            label="Bad",
            field_type="text",
        )
    with pytest.raises(ValidationError):
        CustomFieldDefinitionIn(
            entity_type="job",
            field_key="1leading_digit",
            label="Bad",
            field_type="text",
        )
    with pytest.raises(ValidationError):
        CustomFieldDefinitionIn(
            entity_type="job",
            field_key="has space",
            label="Bad",
            field_type="text",
        )
    # And entity_type outside the enum
    with pytest.raises(ValidationError):
        CustomFieldDefinitionIn(
            entity_type="invoice",
            field_key="x",
            label="x",
            field_type="text",
        )


def test_set_job_values(session_factory):
    db = session_factory()
    try:
        cf.create_definition(
            payload=CustomFieldDefinitionIn(
                entity_type="job",
                field_key="gate_code",
                label="Gate Code",
                field_type="text",
            ),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        result = cf.put_job_custom_fields(
            job_id="job-123",
            payload=CustomFieldValueUpsert(values={"gate_code": "1234#"}),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        assert len(result) == 1
        assert result[0]["field_key"] == "gate_code"
        assert result[0]["value"] == "1234#"

        # GET returns the same
        read = cf.get_job_custom_fields(
            job_id="job-123",
            request=_request("tenant-a"),
            _=_TEST_USER,
            db=db,
        )
        assert len(read) == 1
        assert read[0]["value"] == "1234#"

        # Upsert overwrites
        updated = cf.put_job_custom_fields(
            job_id="job-123",
            payload=CustomFieldValueUpsert(values={"gate_code": "9999"}),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        assert updated[0]["value"] == "9999"
    finally:
        db.close()


def test_set_customer_values(session_factory):
    db = session_factory()
    try:
        cf.create_definition(
            payload=CustomFieldDefinitionIn(
                entity_type="customer",
                field_key="vip",
                label="VIP",
                field_type="boolean",
            ),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        result = cf.put_customer_custom_fields(
            customer_id="cust-42",
            payload=CustomFieldValueUpsert(values={"vip": True}),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        assert len(result) == 1
        assert result[0]["field_key"] == "vip"
        assert result[0]["value"] == "true"

        # Unknown field_key rejected
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            cf.put_customer_custom_fields(
                customer_id="cust-42",
                payload=CustomFieldValueUpsert(values={"nonexistent": "x"}),
                request=_request("tenant-a"),
                user=_TEST_USER,
                db=db,
            )
        assert exc_info.value.status_code == 422
    finally:
        db.close()


def test_soft_delete_definition(session_factory):
    db = session_factory()
    try:
        created = cf.create_definition(
            payload=CustomFieldDefinitionIn(
                entity_type="job",
                field_key="to_delete",
                label="Temp",
                field_type="text",
            ),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )
        def_id = created["id"]

        # Confirm present
        before = cf.list_definitions(
            request=_request("tenant-a"), _=_TEST_USER, db=db, entity_type="job"
        )
        assert any(d["id"] == def_id for d in before)

        # Soft delete
        from uuid import UUID as _UUID
        cf.delete_definition(
            definition_id=_UUID(def_id),
            request=_request("tenant-a"),
            user=_TEST_USER,
            db=db,
        )

        after = cf.list_definitions(
            request=_request("tenant-a"), _=_TEST_USER, db=db, entity_type="job"
        )
        assert not any(d["id"] == def_id for d in after)
    finally:
        db.close()
