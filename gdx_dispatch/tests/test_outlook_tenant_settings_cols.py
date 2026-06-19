"""Slice outlook-s2 — verify the 4 new TenantSettings columns exist + are nullable."""
from __future__ import annotations

from gdx_dispatch.control.models import TenantSettings


def test_outlook_columns_exist():
    cols = {c.name for c in TenantSettings.__table__.columns}
    for name in (
        "outlook_microsoft_tenant_id",
        "outlook_client_id",
        "outlook_client_secret_enc",
        "outlook_secret_set_at",
    ):
        assert name in cols, f"missing column: {name}"


def test_outlook_columns_are_nullable():
    """Brand-new tenants haven't connected Outlook yet — every column starts NULL."""
    for name in (
        "outlook_microsoft_tenant_id",
        "outlook_client_id",
        "outlook_client_secret_enc",
        "outlook_secret_set_at",
    ):
        col = TenantSettings.__table__.columns[name]
        assert col.nullable is True, f"{name} must be nullable"


def test_outlook_secret_column_is_text_for_fernet_ciphertext():
    """Fernet ciphertext is base64 + sometimes padded; Text avoids length issues."""
    col = TenantSettings.__table__.columns["outlook_client_secret_enc"]
    # Text columns have no length on Postgres; on sqlite, they map to TEXT.
    # We check the SQLAlchemy type isn't a length-bounded String.
    from sqlalchemy import Text, String
    assert isinstance(col.type, Text) or (isinstance(col.type, String) and col.type.length is None), (
        f"outlook_client_secret_enc must be Text (Fernet ciphertext is variable-length); got {col.type}"
    )
