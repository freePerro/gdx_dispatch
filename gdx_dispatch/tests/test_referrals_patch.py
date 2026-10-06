"""PATCH /api/referrals/{id} — the audit row keeps the status it moved from."""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import LoyaltyReferral
from gdx_dispatch.routers.referrals import ReferralPatchIn, patch_referral


def test_patch_referral_audit_records_prior_status():
    """GDXA-333: the status change logged only the new status."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    db.add(LoyaltyReferral(
        id="ref-1", company_id="t1", referrer_id="c1",
        referee_name="Pat", referee_phone="555-0101", status="pending",
    ))
    db.commit()

    out = patch_referral(
        referral_id="ref-1",
        payload=ReferralPatchIn(status="converted"),
        request=SimpleNamespace(state=SimpleNamespace(tenant={"id": "t1"}), headers={}, client=None),
        user={"sub": "user-1"},
        db=db,
    )

    assert out["status"] == "converted"
    (raw,) = db.execute(
        text("SELECT details FROM audit_logs WHERE action = 'referral_updated'")
    ).one()
    details = raw if isinstance(raw, dict) else json.loads(raw)
    assert details == {"status": "converted", "from": "pending"}
    db.close()
    engine.dispose()
