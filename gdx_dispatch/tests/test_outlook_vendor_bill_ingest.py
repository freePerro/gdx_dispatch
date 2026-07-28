"""Outlook → vendor-bills delta-ingest bridge (Phase 2).

Pure-logic tests: the Graph client and the vendor-invoice pipeline are both
mocked, so no network, no DB, no real PDF.
"""
from __future__ import annotations

from types import SimpleNamespace

from gdx_dispatch.modules.outlook import vendor_bill_ingest as vbi
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import MidwestInvoiceParseError
from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    MidwestParseError as MidwestStatementParseError,
)


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


# --------------------------------------------------------------------------- #
# LLM rung 2 (D4) — parser-rejected PDFs go to Claude-vision, bounded
# --------------------------------------------------------------------------- #
def _parse_fails(*a, **k):
    raise MidwestInvoiceParseError("not a midwest invoice")


def test_llm_rung_ingests_parser_rejected_pdf(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)
    llm_calls = []

    def fake_llm_upload(tdb, *, pdf_bytes, llm_client, **k):
        llm_calls.append(pdf_bytes)
        return SimpleNamespace(created=True)

    monkeypatch.setattr(vbi, "upload_invoice_via_llm", fake_llm_upload)
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"scan"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object(),
    )
    assert result["ingested"] == 1
    assert result["ingested_llm"] == 1
    assert result["llm_extractions"] == 1
    assert result["unparseable"] == 0
    assert llm_calls == [b"scan"]


def test_llm_rung_off_without_client_keeps_old_behavior(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)
    called = []
    monkeypatch.setattr(vbi, "upload_invoice_via_llm", lambda *a, **k: called.append(1))
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"scan"})
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])
    assert result["unparseable"] == 1
    assert result["llm_extractions"] == 0
    assert called == []


def test_llm_rung_cost_ceiling_caps_and_flags(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)
    monkeypatch.setattr(
        vbi, "upload_invoice_via_llm",
        lambda *a, **k: SimpleNamespace(created=True),
    )
    gc = _FakeGC(
        [_pdf_att("a1"), _pdf_att("a2")],
        {"a1": b"s1", "a2": b"s2"},
    )
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object(), max_llm_extractions=1,
    )
    assert result["llm_extractions"] == 1
    assert result["ingested_llm"] == 1
    assert result["llm_capped"] == 1        # a2 hit the ceiling → retry later
    assert result["unparseable"] == 0


def test_llm_rung_duplicate_counts_as_duplicate(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)
    monkeypatch.setattr(
        vbi, "upload_invoice_via_llm",
        lambda *a, **k: SimpleNamespace(created=False),
    )
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"s"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object(),
    )
    assert result["duplicate"] == 1
    assert result["ingested"] == 0


def test_llm_rung_extraction_error_is_unparseable(monkeypatch):
    from gdx_dispatch.modules.vendor_invoices.llm_extract import LLMExtractionError

    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)

    def boom(*a, **k):
        raise LLMExtractionError("model couldn't read it")

    monkeypatch.setattr(vbi, "upload_invoice_via_llm", boom)
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"s"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object(),
    )
    assert result["unparseable"] == 1
    assert result["errors"] == 0            # deterministic — do NOT retry


def test_llm_rung_api_failure_is_retryable_error(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)

    def boom(*a, **k):
        raise RuntimeError("anthropic 529 overloaded")

    monkeypatch.setattr(vbi, "upload_invoice_via_llm", boom)
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"s"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object(),
    )
    assert result["errors"] == 1            # blocks checkpoint → retried
    assert result["unparseable"] == 0


# --------------------------------------------------------------------------- #
# LLM rung — real SDK exception taxonomy (audit fix: deterministic 4xx must
# not loop as retryable forever)
# --------------------------------------------------------------------------- #
def _anthropic_error(cls, status):
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req)
    return getattr(anthropic, cls)("boom", response=resp, body=None)


def test_llm_rung_deterministic_400_is_unparseable_not_retryable(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)

    def boom(*a, **k):
        raise _anthropic_error("BadRequestError", 400)  # encrypted / >100 pages

    monkeypatch.setattr(vbi, "upload_invoice_via_llm", boom)
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"s"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object(),
    )
    assert result["unparseable"] == 1       # stamped — never re-burns budget
    assert result["errors"] == 0


def test_llm_rung_auth_and_ratelimit_and_5xx_are_retryable(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)
    for cls, status in [("AuthenticationError", 401),
                        ("RateLimitError", 429),
                        ("InternalServerError", 500)]:
        def boom(*a, _c=cls, _s=status, **k):
            raise _anthropic_error(_c, _s)

        monkeypatch.setattr(vbi, "upload_invoice_via_llm", boom)
        gc = _FakeGC([_pdf_att("a1")], {"a1": b"s"})
        result = vbi.ingest_message_attachments(
            None, gc, _msg(), ["midwest.com"], llm_client=object(),
        )
        assert result["errors"] == 1, f"{cls} must be retryable"
        assert result["unparseable"] == 0, f"{cls} must not stamp"


def test_llm_rung_broken_client_counts_error_without_api_call(monkeypatch):
    monkeypatch.setattr(vbi, "upload_midwest_invoice", _parse_fails)
    called = []
    monkeypatch.setattr(vbi, "upload_invoice_via_llm", lambda *a, **k: called.append(1))
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"s"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=vbi.LLM_BROKEN,
    )
    assert result["errors"] == 1            # retryable after the key is fixed
    assert result["unparseable"] == 0
    assert result["llm_extractions"] == 0
    assert called == []


# --------------------------------------------------------------------------- #
# rung 1b — vendor statements of account
#
# A statement is not a payable. Before this rung existed the ladder ran
# invoice-parser -> LLM, and the LLM's classifier (correctly) refused to record
# a statement as a bill — so every statement the vendor emailed was counted
# "unparseable" and silently dropped. These tests pin the routing.
# --------------------------------------------------------------------------- #
def _stmt_result(created=True, sid="s1", lines=27, sdate="2026-05-03"):
    return SimpleNamespace(
        created=created,
        statement=SimpleNamespace(id=sid, statement_date=sdate, line_count=lines),
    )


def _only_statements_parse(monkeypatch, *, statement_bytes=b"stmt"):
    """Invoice parser rejects everything; statement parser claims statement_bytes."""
    def fake_invoice(tdb, *, pdf_bytes, **k):
        raise MidwestInvoiceParseError("not a midwest invoice")

    def fake_statement(tdb, *, pdf_bytes, **k):
        if pdf_bytes != statement_bytes:
            raise MidwestStatementParseError("not a midwest statement")
        return _stmt_result()

    monkeypatch.setattr(vbi, "upload_midwest_invoice", fake_invoice)
    monkeypatch.setattr(vbi, "ingest_midwest_statement", fake_statement)


def test_statement_pdf_is_recorded_as_a_statement_not_a_bill(monkeypatch):
    _only_statements_parse(monkeypatch)
    seen = []
    monkeypatch.setattr(vbi, "upload_invoice_via_llm", lambda *a, **k: seen.append(1))

    gc = _FakeGC([_pdf_att("a1", "statement.pdf")], {"a1": b"stmt"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object()
    )

    assert result["statements"] == 1
    # The whole point: it must NOT be counted as a bill, and must NOT be
    # counted unparseable (the pre-fix behavior that lost them).
    assert result["ingested"] == 0
    assert result["unparseable"] == 0
    assert result["errors"] == 0
    # Deterministic rung claimed it — no tokens spent.
    assert seen == []
    assert result["llm_extractions"] == 0


def test_statement_rung_passes_source_email_and_the_pdf_bytes(monkeypatch):
    calls = []

    def fake_statement(tdb, *, pdf_bytes, original_filename, content_type, uploaded_by, source):
        calls.append({"bytes": pdf_bytes, "name": original_filename, "source": source})
        return _stmt_result()

    monkeypatch.setattr(
        vbi, "upload_midwest_invoice",
        lambda tdb, **k: (_ for _ in ()).throw(MidwestInvoiceParseError("no")),
    )
    monkeypatch.setattr(vbi, "ingest_midwest_statement", fake_statement)

    gc = _FakeGC([_pdf_att("a1", "cs_master.PDF")], {"a1": b"stmt"})
    vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])

    assert calls[0]["source"] == "email"
    assert calls[0]["bytes"] == b"stmt"
    assert calls[0]["name"] == "cs_master.PDF"


def test_already_known_statement_counts_duplicate_not_new(monkeypatch):
    monkeypatch.setattr(
        vbi, "upload_midwest_invoice",
        lambda tdb, **k: (_ for _ in ()).throw(MidwestInvoiceParseError("no")),
    )
    monkeypatch.setattr(
        vbi, "ingest_midwest_statement", lambda tdb, **k: _stmt_result(created=False)
    )

    gc = _FakeGC([_pdf_att("a1")], {"a1": b"stmt"})
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])

    assert result["statement_duplicate"] == 1
    assert result["statements"] == 0
    assert result["duplicate"] == 0   # bill duplicates are a separate count
    assert result["errors"] == 0


def test_non_statement_still_falls_through_to_the_llm_rung(monkeypatch):
    """Rung 1b must not swallow documents it can't parse — the LLM still gets
    its turn at a scanned bill from a vendor with no deterministic parser."""
    _only_statements_parse(monkeypatch, statement_bytes=b"stmt")
    llm_calls = []

    def fake_llm(tdb, *, pdf_bytes, **k):
        llm_calls.append(pdf_bytes)
        return SimpleNamespace(created=True)

    monkeypatch.setattr(vbi, "upload_invoice_via_llm", fake_llm)

    gc = _FakeGC([_pdf_att("a1", "scan.pdf")], {"a1": b"a scanned bill"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object()
    )

    assert llm_calls == [b"a scanned bill"]
    assert result["ingested"] == 1
    assert result["ingested_llm"] == 1
    assert result["statements"] == 0


def test_statement_rung_runs_before_the_llm(monkeypatch):
    """Ordering is the cost control: the free deterministic parser must get the
    PDF first, so a statement never reaches a paid extraction."""
    order = []

    def fake_statement(tdb, *, pdf_bytes, **k):
        order.append("statement")
        return _stmt_result()

    def fake_llm(tdb, **k):
        order.append("llm")
        return SimpleNamespace(created=True)

    monkeypatch.setattr(
        vbi, "upload_midwest_invoice",
        lambda tdb, **k: (_ for _ in ()).throw(MidwestInvoiceParseError("no")),
    )
    monkeypatch.setattr(vbi, "ingest_midwest_statement", fake_statement)
    monkeypatch.setattr(vbi, "upload_invoice_via_llm", fake_llm)

    gc = _FakeGC([_pdf_att("a1")], {"a1": b"stmt"})
    vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"], llm_client=object())

    assert order == ["statement"]


def test_statement_storage_failure_is_retryable_and_never_reaches_the_llm(monkeypatch):
    """A DB/disk failure while STORING a statement must not be laundered into
    an LLM bill extraction — that would record the statement as a payable."""
    monkeypatch.setattr(
        vbi, "upload_midwest_invoice",
        lambda tdb, **k: (_ for _ in ()).throw(MidwestInvoiceParseError("no")),
    )
    monkeypatch.setattr(
        vbi, "ingest_midwest_statement",
        lambda tdb, **k: (_ for _ in ()).throw(RuntimeError("db is down")),
    )
    llm_calls = []
    monkeypatch.setattr(vbi, "upload_invoice_via_llm", lambda *a, **k: llm_calls.append(1))

    gc = _FakeGC([_pdf_att("a1")], {"a1": b"stmt"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object()
    )

    assert result["errors"] == 1          # retryable => message stays un-checkpointed
    assert result["statements"] == 0
    assert result["unparseable"] == 0     # NOT a permanent skip
    assert llm_calls == []


def test_statement_rung_off_when_no_llm_key_still_records_statements(monkeypatch):
    """Statements are deterministic — they must land even with rung 2 disabled
    (llm_client=None), which is the tenant's default state."""
    _only_statements_parse(monkeypatch)
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"stmt"})
    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"], llm_client=None)
    assert result["statements"] == 1
    assert result["unparseable"] == 0


# --------------------------------------------------------------------------- #
# rung 1b — "it IS ours and we lost it" must not look like "it isn't ours"
#
# The statement parser is deliberately strict: one unexpected detail line and
# it raises rather than skipping. Before this distinction existed, that raise
# was indistinguishable from "not a statement", so a real statement whose
# layout drifted would fall to the LLM rung, be counted with the junk
# attachments, and get checkpointed — lost silently, forever.
# --------------------------------------------------------------------------- #
def _identified_but_unreadable(monkeypatch):
    from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
        MidwestStatementStructureError,
    )

    monkeypatch.setattr(
        vbi, "upload_midwest_invoice",
        lambda tdb, **k: (_ for _ in ()).throw(MidwestInvoiceParseError("no")),
    )
    monkeypatch.setattr(
        vbi, "ingest_midwest_statement",
        lambda tdb, **k: (_ for _ in ()).throw(
            MidwestStatementStructureError("could not parse line B for invoice 100493")
        ),
    )


def test_unreadable_statement_is_counted_under_its_own_name(monkeypatch):
    _identified_but_unreadable(monkeypatch)
    gc = _FakeGC([_pdf_att("a1", "cs_master.PDF")], {"a1": b"stmt"})

    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])

    assert result["statement_unparseable"] == 1
    # NOT bucketed with junk attachments — that's what made the loss invisible.
    assert result["unparseable"] == 0
    assert result["statements"] == 0


def test_unreadable_statement_never_reaches_the_bill_extractor(monkeypatch):
    """Handing a statement to an invoice extractor is how one gets booked as a
    payable and shadows the real invoice through (vendor, invoice_number)."""
    _identified_but_unreadable(monkeypatch)
    llm_calls = []
    monkeypatch.setattr(vbi, "upload_invoice_via_llm", lambda *a, **k: llm_calls.append(1))

    gc = _FakeGC([_pdf_att("a1")], {"a1": b"stmt"})
    vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"], llm_client=object())

    assert llm_calls == []


def test_unreadable_statement_does_not_block_the_checkpoint(monkeypatch):
    """It must not count as an error either: a parser gap that blocks the
    checkpoint re-downloads the same PDF on every run, forever."""
    _identified_but_unreadable(monkeypatch)
    gc = _FakeGC([_pdf_att("a1")], {"a1": b"stmt"})

    result = vbi.ingest_message_attachments(None, gc, _msg(), ["midwest.com"])

    assert result["errors"] == 0
    assert result["capped"] == 0
    assert result["llm_capped"] == 0


def test_unreadable_statement_is_logged_loudly_enough_to_act_on(monkeypatch, caplog):
    import logging

    _identified_but_unreadable(monkeypatch)
    gc = _FakeGC([_pdf_att("a1", "cs_master.PDF")], {"a1": b"stmt"})

    with caplog.at_level(logging.ERROR):
        vbi.ingest_message_attachments(None, gc, _msg(mid="AAMk-123"), ["midwest.com"])

    assert any(
        r.levelno >= logging.ERROR
        and "cs_master.PDF" in r.getMessage()
        and "AAMk-123" in r.getMessage()
        for r in caplog.records
    ), "a dropped statement must name the file and the message so it can be recovered"


def test_a_pdf_that_is_simply_not_a_statement_still_climbs_to_rung_2(monkeypatch):
    """The distinction must not over-claim: a plain 'not mine' still falls
    through, which is the routine answer on a ladder of parsers."""
    _only_statements_parse(monkeypatch, statement_bytes=b"stmt")
    llm_calls = []
    monkeypatch.setattr(
        vbi, "upload_invoice_via_llm",
        lambda tdb, *, pdf_bytes, **k: llm_calls.append(pdf_bytes) or SimpleNamespace(created=True),
    )

    gc = _FakeGC([_pdf_att("a1", "scan.pdf")], {"a1": b"some other vendor bill"})
    result = vbi.ingest_message_attachments(
        None, gc, _msg(), ["midwest.com"], llm_client=object()
    )

    assert llm_calls == [b"some other vendor bill"]
    assert result["statement_unparseable"] == 0
