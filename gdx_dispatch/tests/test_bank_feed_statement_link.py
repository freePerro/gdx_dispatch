"""The statement-link endpoint, and the "worth prompting about" rule.

Both exist to keep a money surface honest, so both are tested for what they
REFUSE as much as what they do:

* the link is one-to-one, because two feed accounts claiming one statement
  account would both report `statement-verified` for one real transaction;
* the audit line records WHICH pairing was made — an entry reading only
  "statement linked" cannot reconstruct the pairing behind a verdict;
* the prompt counts only accounts worth acting on. A disabled feed, or one
  that has never carried a transaction, has nothing to reconcile, and a badge
  that never reaches zero is a badge nobody reads.

Deliberately absent: any test that a pairing can be *suggested*. An earlier
draft derived one from a last-4 in the account name. `bank_accounts` is unique
on (institution, last4), so two banks can both end 2204, and the two sides
share no institution vocabulary to disambiguate with — on this tenant the feed
says "SimpleFIN Bridge" and the statement says "Primary Bank". The inference
cannot be validated, so it is not offered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from starlette.requests import Request as StarletteRequest

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.modules.bank_feeds import oauth
from gdx_dispatch.modules.bank_feeds import router as r
from gdx_dispatch.modules.bank_feeds.models import (
    BankFeedAccount,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.modules.bank_feeds.statement_models import BankAccount

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"sub": "tester", "tenant_id": COMPANY, "role": "admin"}


def _request():
    scope = {
        "type": "http", "method": "PATCH", "path": "/", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 80), "state": {},
    }
    req = StarletteRequest(scope)
    req.state.tenant = {"id": COMPANY}
    return req


@pytest.fixture
def wired(tenant_db):
    db = tenant_db
    inst = BannoInstitution(fi_host="fi.example", display_label="SimpleFIN Bridge")
    db.add(inst)
    db.commit()
    conn = BannoConnection(
        institution_id=inst.id, fi_host=inst.fi_host, banno_user_id="u1",
        access_token_enc=oauth._encrypt("at"), refresh_token_enc=oauth._encrypt("rt"),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(conn)
    db.commit()
    bank = BankAccount(
        name="Business Checking", kind="checking", institution="Primary Bank", last4="2204"
    )
    db.add(bank)
    db.commit()
    return db, conn, bank


def _feed(db, conn, *, name, ext, sync=True, txns=0):
    acct = BankFeedAccount(
        connection_id=conn.id, external_account_id=ext, name=name, sync_enabled=sync
    )
    db.add(acct)
    db.commit()
    for i in range(txns):
        db.add(BankFeedTransaction(
            account_id=acct.id, external_transaction_id=f"{ext}-{i}", amount_cents=-100,
        ))
    db.commit()
    return acct


def _accounts(db):
    return r.list_accounts(None, db)


# ── the prompt counts only what is worth acting on ─────────────────────


def test_unlinked_syncing_account_with_transactions_is_actionable(wired):
    db, conn, _bank = wired
    _feed(db, conn, name="Garage Door inc (2204)", ext="a1", txns=3)
    out = _accounts(db)
    assert out["unlinked_actionable_count"] == 1
    assert out["accounts"][0]["needs_statement_link"] is True
    assert out["accounts"][0]["transaction_count"] == 3


def test_a_disabled_feed_is_not_a_to_do(wired):
    db, conn, _bank = wired
    _feed(db, conn, name="Old Banno", ext="a2", sync=False, txns=5)
    out = _accounts(db)
    assert out["unlinked_actionable_count"] == 0
    assert out["accounts"][0]["needs_statement_link"] is False


def test_an_account_with_no_transactions_is_not_a_to_do(wired):
    db, conn, _bank = wired
    _feed(db, conn, name="Empty (9999)", ext="a3", txns=0)
    out = _accounts(db)
    assert out["unlinked_actionable_count"] == 0


def test_linking_clears_it_from_the_prompt(wired):
    db, conn, bank = wired
    acct = _feed(db, conn, name="Garage Door inc (2204)", ext="a4", txns=2)
    assert _accounts(db)["unlinked_actionable_count"] == 1
    r.patch_account_statement_link(
        str(acct.id), r.StatementLinkPatch(bank_account_id=str(bank.id)),
        _request(), USER, None, db,
    )
    out = _accounts(db)
    assert out["unlinked_actionable_count"] == 0
    assert out["accounts"][0]["bank_account_label"] == "Business Checking ····2204"


def test_no_suggestion_is_ever_offered(wired):
    """The payload must not carry a proposed pairing — see the module
    docstring for why a last-4 in a display name is not evidence."""
    db, conn, _bank = wired
    _feed(db, conn, name="Garage Door inc (2204)", ext="a5", txns=1)
    row = _accounts(db)["accounts"][0]
    assert "suggested_bank_account_id" not in row
    assert "suggested_bank_account_label" not in row


# ── the audit line has to say what changed ─────────────────────────────


def test_link_audit_records_the_pairing(wired):
    db, conn, bank = wired
    acct = _feed(db, conn, name="Garage Door inc (2204)", ext="a6", txns=1)
    r.patch_account_statement_link(
        str(acct.id), r.StatementLinkPatch(bank_account_id=str(bank.id)),
        _request(), USER, None, db,
    )
    row = db.scalars(
        select(AuditLog).where(AuditLog.action == "bank_feeds_account_statement_linked")
    ).first()
    assert row is not None
    assert row.details["bank_account_id"] == str(bank.id)
    assert row.details["bank_account"] == "Business Checking ····2204"
    assert row.details["previous_bank_account_id"] is None


def test_unlink_audit_records_what_it_was(wired):
    db, conn, bank = wired
    acct = _feed(db, conn, name="Garage Door inc (2204)", ext="a7", txns=1)
    r.patch_account_statement_link(
        str(acct.id), r.StatementLinkPatch(bank_account_id=str(bank.id)),
        _request(), USER, None, db,
    )
    r.patch_account_statement_link(
        str(acct.id), r.StatementLinkPatch(bank_account_id=None),
        _request(), USER, None, db,
    )
    row = db.scalars(
        select(AuditLog).where(AuditLog.action == "bank_feeds_account_statement_unlinked")
    ).first()
    assert row is not None
    assert row.details["previous_bank_account_id"] == str(bank.id)
    assert row.details["bank_account_id"] is None
