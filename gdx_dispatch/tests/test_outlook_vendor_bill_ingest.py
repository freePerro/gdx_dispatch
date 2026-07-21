"""Outlook → vendor-bills delta-ingest bridge (Phase 2).

Pure-logic tests: the Graph client and the vendor-invoice pipeline are both
mocked, so no network, no DB, no real PDF.
"""
from __future__ import annotations

from types import SimpleNamespace

from gdx_dispatch.modules.outlook import vendor_bill_ingest as vbi
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import MidwestInvoiceParseError


# --------------------------------------------------------------------------- #
# sender_allowed / is_pdf_attachment
# --------------------------------------------------------------------------- #
def test_sender_allowed_by_exact_address_and_domain():
    al = vbi.normalize_allowlist(["billing@Midwest.com", "SupplierCo.com"])
    assert vbi.sender_allowed("billing@midwest.com", al) is True        # exact
    assert vbi.sender_allowed("ar@supplierco.com", al) is True          # domain
    assert vbi.sender_allowed("ar@mail.supplierco.com", al) is True     # subdomain
    assert vbi.sender_allowed("someone@evil.com", al) is False
    assert vbi.sender_allowed(None, al) is False
    assert vbi.sender_allowed("x@midwest.com", []) is False             # empty allowlist


def test_is_pdf_attachment():
    pdf = {"@odata.type": "#microsoft.graph.fileAttachment", "contentType": "application/pdf", "name": "bill.pdf"}
    byname = {"@odata.type": "#microsoft.graph.fileAttachment", "contentType": "application/octet-stream", "name": "b.PDF"}
    png = {"@odata.type": "#microsoft.graph.fileAttachment", "contentType": "image/png", "name": "logo.png"}
    item = {"@odata.type": "#microsoft.graph.itemAttachment", "name": "fwd.eml"}
    assert vbi.is_pdf_attachment(pdf) is True
    assert vbi.is_pdf_attachment(byname) is True
    assert vbi.is_pdf_attachment(png) is False
    assert vbi.is_pdf_attachment(item) is False


# --------------------------------------------------------------------------- #
# ingest_message_attachments
# --------------------------------------------------------------------------- #
class _FakeGC:
    def __init__(self, attachments, bytes_map):
        self._attachments = attachments
        self._bytes = bytes_map
        self.downloads = []

    def list_attachments(self, msg_id):
        return self._attachments

    def download_attachment(self, msg_id, att_id):
        self.downloads.append(att_id)
        return self._bytes[att_id]


def _msg(sender="billing@midwest.com", has_attachments=True, mid="m1"):
    return {
        "id": mid,
        "hasAttachments": has_attachments,
        "from": {"emailAddress": {"address": sender}},
    }


def _pdf_att(att_id="a1", name="bill.pdf"):
    return {"@odata.type": "#microsoft.graph.fileAttachment", "id": att_id,
            "contentType": "application/pdf", "name": name}


def test_ingest_allowlisted_pdf_calls_pipeline_with_source_email(monkeypatch):
    calls = []

    def fake_upload(tdb, *, pdf_bytes, original_filename, content_type, uploaded_by, source):
        calls.append({"bytes": pdf_bytes, "name": original_filename, "source": source})
        return SimpleNamespace(created=True)

    monkeypatch.setattr(vbi, "upload_midwest_invoice", fake_upload)
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"%PDF-1.4 fake"})

    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])

    assert result["ingested"] == 1
    assert calls[0]["source"] == "email"
    assert calls[0]["bytes"] == b"%PDF-1.4 fake"
    assert gc.downloads == ["a1"]


def test_ingest_skips_non_allowlisted_sender(monkeypatch):
    called = []
    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: called.append(1))
    gc = _FakeGC([_pdf_att()], {"a1": b"x"})
    result = vbi.ingest_message_attachments(None, gc, _msg(sender="ar@stranger.com"), ["midwest.com"])
    assert result == vbi.new_totals()
    assert called == []


def test_ingest_empty_allowlist_is_noop(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: SimpleNamespace(created=True))
    gc = _FakeGC([_pdf_att()], {"a1": b"x"})
    assert vbi.ingest_message_attachments(None, gc, _msg(), [])["ingested"] == 0


def test_ingest_ignores_non_pdf_attachments(monkeypatch):
    called = []
    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: called.append(1) or SimpleNamespace(created=True))
    png = {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "contentType": "image/png", "name": "x.png"}
    gc = _FakeGC([png], {"a1": b"x"})
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])
    assert result["ingested"] == 0
    assert called == []


def test_ingest_counts_duplicate_and_unparseable(monkeypatch):
    def fake_upload(tdb, *, pdf_bytes, **k):
        if pdf_bytes == b"dup":
            return SimpleNamespace(created=False)      # content-hash dedup
        raise MidwestInvoiceParseError("not a midwest invoice")

    monkeypatch.setattr(vbi, "upload_midwest_invoice", fake_upload)
    gc = _FakeGC([_pdf_att("a1", "dup.pdf"), _pdf_att("a2", "scan.pdf")], {"a1": b"dup", "a2": b"scan"})
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])
    assert result["duplicate"] == 1
    assert result["unparseable"] == 1
    assert result["ingested"] == 0


def test_ingest_message_without_attachments_is_noop(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: SimpleNamespace(created=True))
    gc = _FakeGC([], {})
    assert vbi.ingest_message_attachments(None, gc, _msg(has_attachments=False), ["midwest.com"])["ingested"] == 0


# --------------------------------------------------------------------------- #
# is_candidate (cheap pre-filter used by the sync to collect, not ingest inline)
# --------------------------------------------------------------------------- #
def test_is_candidate():
    al = ["midwest.com"]
    assert vbi.is_candidate(_msg(), al) is True
    assert vbi.is_candidate(_msg(sender="x@stranger.com"), al) is False
    assert vbi.is_candidate(_msg(has_attachments=False), al) is False
    assert vbi.is_candidate(_msg(), []) is False  # feature off


def test_isolated_ingest_helper_short_circuits_without_touching_a_session():
    # The transaction-isolation fix: ingest runs in a SEPARATE session AFTER the
    # folder sync commits. With no candidates it returns zeros without opening
    # one (so this is safe to call even with no DB configured).
    from gdx_dispatch.modules.outlook.tasks import _ingest_vendor_bills
    assert _ingest_vendor_bills(None, [], ["midwest.com"]) == {
        **vbi.new_totals(),
        "skipped_no_budget": 0,
        "skipped_already_ingested": 0,
        "quarantined": 0,
    }


# --------------------------------------------------------------------------- #
# max_downloads budget (D3 — the sweep's per-run download cap)
# --------------------------------------------------------------------------- #
def test_ingest_budget_cuts_message_short_and_flags_capped(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: SimpleNamespace(created=True))
    gc = _FakeGC(
        [_pdf_att("a1"), _pdf_att("a2"), _pdf_att("a3")],
        {"a1": b"1", "a2": b"2", "a3": b"3"},
    )
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"], max_downloads=2)
    assert result["downloads"] == 2
    assert result["ingested"] == 2
    assert result["capped"] == 1          # a3 was never fetched
    assert gc.downloads == ["a1", "a2"]


def test_ingest_zero_budget_downloads_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: called.append(1))
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"1"})
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"], max_downloads=0)
    assert result["downloads"] == 0
    assert result["capped"] == 1
    assert called == []
    assert gc.downloads == []


def test_ingest_failed_download_still_consumes_budget(monkeypatch):
    from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError

    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: SimpleNamespace(created=True))

    class _FlakyGC(_FakeGC):
        def download_attachment(self, msg_id, att_id):
            if att_id == "a1":
                self.downloads.append(att_id)
                raise OutlookGraphAPIError(500, "boom")
            return super().download_attachment(msg_id, att_id)

    gc = _FlakyGC([_pdf_att("a1"), _pdf_att("a2")], {"a2": b"2"})
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"], max_downloads=2)
    # The failed attempt spent a Graph call: budget counts it.
    assert result["downloads"] == 2
    assert result["errors"] == 1
    assert result["ingested"] == 1
    assert result["capped"] == 0


def test_ingest_no_budget_means_unlimited(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", lambda *a, **k: SimpleNamespace(created=True))
    gc = _FakeGC(
        [_pdf_att("a1"), _pdf_att("a2"), _pdf_att("a3")],
        {"a1": b"1", "a2": b"2", "a3": b"3"},
    )
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])
    assert result["downloads"] == 3
    assert result["ingested"] == 3
    assert result["capped"] == 0
