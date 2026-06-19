"""pc-s6a — backfill legacy NULL Customer.*_hash rows."""
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.pii import HashColumn
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.tools.backfill_customer_phone_hash import run_backfill_on_engine


def _engine_with_null_hashes():
    """Sqlite engine with 3 Customer rows that have NULL hashes — bypasses
    the @validates decorator by using a raw SQL INSERT."""
    engine = create_engine("sqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    with engine.begin() as conn:
        for i, phone in enumerate(["+13202959628", "+15551234567", None]):
            phone_sql = "NULL" if phone is None else f"'{phone}'"
            # 32 hex chars no dashes — Uuid(as_uuid=True) stores as CHAR(32) on sqlite.
            uid = f"0000000000000000000000000000000{i}"
            sql = (
                f"INSERT INTO customers (id, name, phone, company_id) "  # noqa: S608
                f"VALUES ('{uid}', 'Cust{i}', {phone_sql}, 't1')"
            )
            conn.execute(text(sql))
    return engine


def test_backfill_populates_null_phone_hashes():
    engine = _engine_with_null_hashes()
    Session = sessionmaker(bind=engine)

    # Pre-state: phone_hash NULL on rows where phone is set
    sess = Session()
    rows_pre = sess.query(Customer).order_by(Customer.id).all()
    assert rows_pre[0].phone_hash is None
    assert rows_pre[0].phone == "+13202959628"
    sess.close()

    # Run backfill
    result = run_backfill_on_engine(engine, tenant_slug="t1")
    assert result["updated"] >= 2  # 2 rows had non-null phone

    # Post-state: hashes populated for rows with phone, NULL preserved otherwise
    sess = Session()
    rows = sess.query(Customer).order_by(Customer.id).all()
    assert rows[0].phone_hash == HashColumn.hash_for_search("+13202959628")
    assert rows[1].phone_hash == HashColumn.hash_for_search("+15551234567")
    assert rows[2].phone_hash is None  # phone was None; hash stays None
    sess.close()


def test_backfill_idempotent_re_run():
    engine = _engine_with_null_hashes()
    run_backfill_on_engine(engine, tenant_slug="t1")
    second = run_backfill_on_engine(engine, tenant_slug="t1")
    assert second["updated"] == 0  # nothing left to fix


def test_backfill_refuses_control_plane(tmp_path):
    """Crude check: refuse to run if the engine URL contains 'gdx_control'."""
    import pytest
    engine = create_engine("sqlite:///" + str(tmp_path / "gdx_control_test.db"))
    TenantBase.metadata.create_all(engine)
    with pytest.raises(Exception, match="control"):
        run_backfill_on_engine(engine, tenant_slug="control")
