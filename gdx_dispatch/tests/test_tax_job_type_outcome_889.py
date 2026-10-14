"""Every job_type pinned to its sales-tax outcome (GDXA-889).

Doug, 2026-10-13 (GDXA-840, option A): New Construction is an installation for
tax, so it is never taxed, like Installation. It stays in the "office" PRICING
lane: the tax predicate is wider than the pricing lane on purpose, so New
Construction is never auto-priced flat. Every other job type, NULL and free
text are taxed at the tenant default: a work kind is never guessed from free
text. Exempt customers are never taxed, on any job.

Real invocations against a SQLite session built from the ORM, no mocks.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.job_taxonomy import JOB_TYPE_OPTIONS, QB_IMPORT, pricing_lane
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.service import is_install_lane, resolve_rate_for_job

DEFAULT = Decimal("0.07375")
ZERO = Decimal("0")

# job_type → untaxed? Every canonical option, QB Import, NULL, legacy aliases
# and free text. A new option added to JOB_TYPE_OPTIONS without a row here
# fails test_every_option_is_pinned.
OUTCOMES: dict[str | None, bool] = {
    "Service Call": False,
    "Installation": True,
    "Repair": False,
    "Maintenance": False,
    "New Construction": True,
    "Inspection": False,
    "Other": False,
    QB_IMPORT: False,
    None: False,
    "": False,
    # Legacy spellings fold through canonical_job_type.
    "install": True,
    "INSTALLATION": True,
    "new construction": True,
    "  New   Construction ": True,
    "Service": False,
    # Free text is never read as a work kind.
    "Garage Door Install": False,
    "New Construction Install": False,
    "Construction": False,
}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    sess = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    sess.add(TaxConfig(name="Default", default_rate=DEFAULT))
    sess.commit()
    yield sess
    sess.close()
    engine.dispose()


def test_every_option_is_pinned():
    assert set(JOB_TYPE_OPTIONS) <= set(OUTCOMES)


@pytest.mark.parametrize(("job_type", "untaxed"), list(OUTCOMES.items()))
def test_job_type_tax_outcome(db, job_type, untaxed):
    assert is_install_lane(job_type) is untaxed
    expected = ZERO if untaxed else DEFAULT
    assert resolve_rate_for_job(db, uuid4(), job_type) == expected
    # A quote cannot tax an installation; elsewhere the quote stands.
    quoted = resolve_rate_for_job(db, uuid4(), job_type, quoted_rate=Decimal("0.05"))
    assert quoted == (ZERO if untaxed else Decimal("0.05"))


@pytest.mark.parametrize("job_type", list(OUTCOMES))
def test_exempt_customer_is_never_taxed(db, job_type):
    customer_id = uuid4()
    db.add(TaxExemption(customer_id=customer_id, exempt=True, reason="non-profit"))
    db.commit()
    assert resolve_rate_for_job(db, customer_id, job_type) == ZERO
    assert resolve_rate_for_job(db, customer_id, job_type, quoted_rate=Decimal("0.05")) == ZERO


def test_new_construction_stays_office_priced():
    """Tax-only: New Construction must not join the install PRICING lane,
    which would auto-price it flat from the matrix."""
    assert pricing_lane("New Construction") == "office"
    assert pricing_lane("Installation") == "install"
