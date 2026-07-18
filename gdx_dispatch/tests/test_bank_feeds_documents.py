"""Bank feeds documents sweep — eligibility probe semantics, metadata-first
retry queue, cursor-only-on-complete, path-guarded download."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from gdx_dispatch.modules.bank_feeds import oauth, service
from gdx_dispatch.modules.bank_feeds.client import BannoClient
from gdx_dispatch.modules.bank_feeds.models import (
    BankFeedDocument,
    BannoConnection,
    BannoInstitution,
)

FI_HOST = "digital.garden-fi.com"
SUB = "sub-1"
BASE = f"https://{FI_HOST}/a/consumer/api/v0/users/{SUB}"
SETTINGS_URL = f"{BASE}/documents/settings/institution"
LIST_URL = f"{BASE}/documents/all"

PDF_BYTES = b"%PDF-1.4 fake statement"


@pytest.fixture
def setup(tenant_db, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    inst = BannoInstitution(fi_host=FI_HOST, display_label="Garden", client_id="cid",
                            client_secret_enc=oauth._encrypt("s"))
    tenant_db.add(inst)
    tenant_db.commit()
    conn = BannoConnection(
        institution_id=inst.id, fi_host=FI_HOST, banno_user_id=SUB,
        access_token_enc=oauth._encrypt("tok"), refresh_token_enc=oauth._encrypt("rt"),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    tenant_db.add(conn)
    tenant_db.commit()
    tenant_db.refresh(conn)
    return conn, tmp_path


def _client():
    return BannoClient(FI_HOST, lambda stale_token=None: "tok")


def _settings_ok():
    return Response(200, json={"settings": {
        "documentTypes": ["statement", "notice", "tax"],
        "documentStartDate": "2020-01-01",
        "maximumConcurrentAccounts": 20,
        "supportedNotificationMethods": [],
        "documentsNativeEnrollmentSSO": False,
        "documentsTitle": "eStatements",
    }})


def _doc(i: int, *, doc_type: str = "statement", account_ids: list | None = None):
    return {
        "documentId": f"doc-{i}",
        "accountIds": account_ids if account_ids is not None else ["acct-1"],
        "documentType": doc_type,
        "documentTitle": f"Statement {i}",
        "documentFilename": f"statement-{i}.pdf",
        "date": "2026-06-30",
    }


@respx.mock
def test_probe_403_sets_unavailable_but_not_sticky(respx_mock, tenant_db, setup):
    conn, _ = setup
    respx_mock.get(SETTINGS_URL).mock(return_value=Response(403, json={"error": "nope"}))
    stats = service.sync_documents(tenant_db, _client(), conn, backfill_days=365)
    assert stats["skipped"] is True
    tenant_db.refresh(conn)
    assert conn.documents_available is False

    # Later the user enrolls — force_probe (manual Sync Now) re-probes.
    respx_mock.get(SETTINGS_URL).mock(side_effect=None, return_value=_settings_ok())
    respx_mock.get(LIST_URL).mock(
        return_value=Response(200, json={"documents": []})
    )
    stats2 = service.sync_documents(tenant_db, _client(), conn, backfill_days=365, force_probe=True)
    assert stats2["skipped"] is False
    tenant_db.refresh(conn)
    assert conn.documents_available is True


@respx.mock
def test_unavailable_short_circuits_without_force(respx_mock, tenant_db, setup):
    conn, _ = setup
    conn.documents_available = False
    tenant_db.commit()
    # No routes mocked — any HTTP call would fail the test.
    stats = service.sync_documents(tenant_db, _client(), conn, backfill_days=365)
    assert stats["skipped"] is True


@respx.mock
def test_happy_sweep_downloads_and_advances_cursor(respx_mock, tenant_db, setup):
    conn, tmp_path = setup
    respx_mock.get(SETTINGS_URL).mock(return_value=_settings_ok())
    respx_mock.get(LIST_URL).mock(return_value=Response(200, json={
        # doc-2 is user-scoped (empty accountIds) — captured by the
        # UNFILTERED listing (audited plan S11).
        "documents": [_doc(1), _doc(2, doc_type="tax", account_ids=[])],
    }))
    respx_mock.get(f"{BASE}/documents/doc-1").mock(
        return_value=Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
    )
    respx_mock.get(f"{BASE}/documents/doc-2").mock(
        return_value=Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
    )

    stats = service.sync_documents(tenant_db, _client(), conn, backfill_days=365)
    assert stats == {"listed": 2, "downloaded": 2, "failed": 0, "skipped": False}

    rows = tenant_db.execute(select(BankFeedDocument)).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert row.fetched_at is not None
        assert row.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
        assert row.size_bytes == len(PDF_BYTES)
        assert row.storage_path and row.storage_path.startswith(str(tmp_path))
    tenant_db.refresh(conn)
    assert conn.documents_synced_through == datetime.now(timezone.utc).date()


@respx.mock
def test_partial_download_failure_leaves_cursor_and_retries(respx_mock, tenant_db, setup):
    conn, _ = setup
    respx_mock.get(SETTINGS_URL).mock(return_value=_settings_ok())
    respx_mock.get(LIST_URL).mock(return_value=Response(200, json={
        "documents": [_doc(1), _doc(2)],
    }))
    respx_mock.get(f"{BASE}/documents/doc-1").mock(
        return_value=Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
    )
    # doc-2 fails persistently (5xx exhausts client retries → BannoAPIError).
    respx_mock.get(f"{BASE}/documents/doc-2").mock(return_value=Response(502))

    stats = service.sync_documents(tenant_db, _client(), conn, backfill_days=365)
    assert stats["downloaded"] == 1
    assert stats["failed"] == 1
    tenant_db.refresh(conn)
    assert conn.documents_synced_through is None  # cursor NOT advanced

    queued = tenant_db.execute(
        select(BankFeedDocument).where(BankFeedDocument.fetched_at.is_(None))
    ).scalars().all()
    assert [d.external_document_id for d in queued] == ["doc-2"]

    # Next sweep: doc-2 now succeeds; only the gap is retried and the
    # cursor advances.
    respx_mock.get(f"{BASE}/documents/doc-2").mock(
        return_value=Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
    )
    stats2 = service.sync_documents(tenant_db, _client(), conn, backfill_days=365)
    assert stats2["downloaded"] == 1
    assert stats2["failed"] == 0
    tenant_db.refresh(conn)
    assert conn.documents_synced_through is not None


@respx.mock
def test_resweep_is_idempotent(respx_mock, tenant_db, setup):
    conn, _ = setup
    respx_mock.get(SETTINGS_URL).mock(return_value=_settings_ok())
    respx_mock.get(LIST_URL).mock(return_value=Response(200, json={"documents": [_doc(1)]}))
    respx_mock.get(f"{BASE}/documents/doc-1").mock(
        return_value=Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
    )
    service.sync_documents(tenant_db, _client(), conn, backfill_days=365)
    service.sync_documents(tenant_db, _client(), conn, backfill_days=365)
    rows = tenant_db.execute(select(BankFeedDocument)).scalars().all()
    assert len(rows) == 1  # unique (connection, external id) held


def test_download_endpoint_path_guard(tenant_db, setup, tmp_path, monkeypatch):
    """A storage_path outside UPLOAD_DIR must 404 (normpath+startswith guard)."""
    from gdx_dispatch.modules.bank_feeds.router import download_document

    conn, _ = setup
    doc = BankFeedDocument(
        connection_id=conn.id, external_document_id="doc-x",
        document_type="statement", storage_path="/etc/passwd",
        fetched_at=datetime.now(timezone.utc),
    )
    tenant_db.add(doc)
    tenant_db.commit()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        download_document(str(doc.id), _perm=None, db=tenant_db)
    assert exc.value.status_code == 404


def test_download_endpoint_serves_guarded_file(tenant_db, setup, tmp_path):
    from gdx_dispatch.modules.bank_feeds.router import download_document

    conn, upload_dir = setup
    target = upload_dir / "bank_statements"
    target.mkdir(parents=True, exist_ok=True)
    pdf = target / "ok.pdf"
    pdf.write_bytes(PDF_BYTES)
    doc = BankFeedDocument(
        connection_id=conn.id, external_document_id="doc-y",
        document_type="statement", storage_path=str(pdf),
        filename="june.pdf", content_type="application/pdf",
        fetched_at=datetime.now(timezone.utc),
    )
    tenant_db.add(doc)
    tenant_db.commit()

    resp = download_document(str(doc.id), _perm=None, db=tenant_db)
    assert resp.path == str(pdf)
    assert resp.media_type == "application/pdf"
