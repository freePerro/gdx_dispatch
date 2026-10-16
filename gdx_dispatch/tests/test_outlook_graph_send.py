"""graph_send: inline under the request cap, draft + upload above it.

Drives the real OutlookGraphClient over an httpx.MockTransport, so what is
pinned is the HTTP that leaves the process: paths, bodies, the upload
session's Content-Range headers, and — load-bearing — that the bearer token
never goes to the pre-authenticated uploadUrl.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from gdx_dispatch.modules.outlook import graph_send
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError, OutlookGraphClient

UPLOAD_URL = "https://outlook.office.com/api/v2.0/Users('u')/Messages('D1')/AttachmentSessions('s')?authtoken=x"


def _att(name: str, size: int) -> dict:
    return {
        "name": name,
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(b"a" * size).decode(),
        "size_bytes": size,
    }


class _Graph:
    """Records every request; answers like Graph does."""

    def __init__(self, *, fail_on: str | None = None, upload_url: str = UPLOAD_URL):
        self.calls: list[httpx.Request] = []
        self.fail_on = fail_on
        self.upload_url = upload_url
        self.received: dict[str, int] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if self.fail_on and self.fail_on in str(req.url):
            return httpx.Response(400, json={"error": {"code": "ErrorInvalidRequest"}})
        if req.method == "PUT":
            start, rest = req.headers["Content-Range"].removeprefix("bytes ").split("-")
            end, total = (int(x) for x in rest.split("/"))
            assert len(req.content) == end - int(start) + 1
            self.received["bytes"] = self.received.get("bytes", 0) + len(req.content)
            return httpx.Response(201 if end + 1 == total else 200, json={})
        if path.endswith("/createUploadSession"):
            return httpx.Response(201, json={"uploadUrl": self.upload_url})
        if req.method == "POST" and path.endswith(("/me/messages", "/createReply", "/createForward")):
            return httpx.Response(201, json={"id": "D1"})
        return httpx.Response(202 if path.endswith(("/send", "/sendMail", "/reply", "/forward")) else 201, json={})

    def lines(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.calls]


@pytest.fixture()
def graph():
    return _Graph()


def _client(handler) -> OutlookGraphClient:
    gc = OutlookGraphClient("secret-token", sleep=lambda _s: None)
    gc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gc


MSG = {"subject": "Quote", "body": {"contentType": "html", "content": "<p>hi</p>"},
       "toRecipients": [{"emailAddress": {"address": "c@example.com"}}]}


def test_small_set_rides_inline_in_one_sendmail(graph):
    route = graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 1000)])
    assert route == "inline"
    assert graph.lines() == ["POST /v1.0/me/sendMail"]
    body = json.loads(graph.calls[0].content)
    assert body["saveToSentItems"] is True
    assert body["message"]["attachments"][0]["name"] == "a.pdf"


def test_no_attachments_is_unchanged_sendmail(graph):
    graph_send.send_new(_client(graph), MSG, None, save_to_sent_items=False)
    assert graph.lines() == ["POST /v1.0/me/sendMail"]
    body = json.loads(graph.calls[0].content)
    assert "attachments" not in body["message"] and body["saveToSentItems"] is False


def test_over_inline_budget_builds_a_draft_and_sends_it(graph):
    atts = [_att("a.pdf", 1_500_000), _att("b.pdf", 1_500_000)]
    route = graph_send.send_new(_client(graph), MSG, atts)
    assert route == "draft"
    assert graph.lines() == [
        "POST /v1.0/me/messages",
        "POST /v1.0/me/messages/D1/attachments",
        "POST /v1.0/me/messages/D1/attachments",
        "POST /v1.0/me/messages/D1/send",
    ]
    draft = json.loads(graph.calls[0].content)
    assert draft["subject"] == "Quote" and "attachments" not in draft


def test_big_file_goes_through_an_upload_session_without_the_token(graph):
    size = 7_000_000  # three chunks at 2,949,120
    graph_send.send_new(_client(graph), MSG, [_att("big.pdf", size)])
    puts = [r for r in graph.calls if r.method == "PUT"]
    assert len(puts) == 3
    assert all("authorization" not in {k.lower() for k in r.headers} for r in puts)
    assert puts[0].headers["Content-Range"] == f"bytes 0-2949119/{size}"
    assert puts[-1].headers["Content-Range"] == f"bytes 5898240-{size - 1}/{size}"
    assert graph.received["bytes"] == size
    session = next(r for r in graph.calls if r.url.path.endswith("/createUploadSession"))
    assert json.loads(session.content)["AttachmentItem"] == {
        "attachmentType": "file", "name": "big.pdf", "size": size, "contentType": "application/pdf",
    }
    # Graph calls carry the bearer; the send happens after the upload.
    assert session.headers["Authorization"] == "Bearer secret-token"
    assert graph.lines()[-1] == "POST /v1.0/me/messages/D1/send"


def test_a_long_body_counts_against_the_inline_budget(graph):
    # Graph's 4 MB cap is the whole request: 2 MB of files plus a ~1M-character
    # quoted body must not be sent as one inline sendMail.
    long = {**MSG, "body": {"contentType": "html", "content": "x" * 1_000_000}}
    assert graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 2_000_000)]) == "inline"
    graph.calls.clear()
    assert graph_send.send_new(_client(graph), long, [_att("a.pdf", 2_000_000)]) == "draft"
    assert graph.lines()[-1] == "POST /v1.0/me/messages/D1/send"


class _Once(_Graph):
    """Answers ``status`` once on the first request whose url contains
    ``match``, then behaves."""

    def __init__(self, match: str, status: int):
        super().__init__()
        self.match, self.status, self.fired = match, status, False

    def __call__(self, req):
        if not self.fired and self.match in str(req.url):
            self.fired = True
            self.calls.append(req)
            return httpx.Response(self.status, json={"error": {"code": "x"}})
        return super().__call__(req)


@pytest.mark.parametrize("match", ["/attachments", "/me/messages", "/D1/send"])
def test_a_504_on_a_call_that_creates_something_is_not_retried(match):
    # Graph may have done the work before answering 504: a retry would attach
    # the file twice, leave an orphan draft, or send the mail twice.
    graph = _Once(match, 504)
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 2_000_000), _att("b.pdf", 1_000_000)])
    hits = [ln for ln in graph.lines() if ln.startswith("POST") and match in ln]
    assert len(hits) == 1


def test_a_504_on_the_one_call_sendmail_is_not_retried():
    graph = _Once("/sendMail", 504)
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 1000)])
    assert graph.lines() == ["POST /v1.0/me/sendMail"]


@pytest.mark.parametrize("match,size", [
    ("/P1/reply", 1000),
    ("/createReply", 3_000_000),
    ("/D1/send", 3_000_000),
])
def test_a_504_on_a_reply_is_not_retried(match, size):
    graph = _Once(match, 504)
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_reply(_client(graph), "P1", MSG, [_att("a.pdf", size)])
    hits = [ln for ln in graph.lines() if ln.startswith("POST") and match in ln]
    assert len(hits) == 1


@pytest.mark.parametrize("match,atts", [
    ("/P1/forward", []),
    ("/createForward", [("a.pdf", 1000)]),
    ("/D1/send", [("a.pdf", 1000)]),
])
def test_a_504_on_a_forward_is_not_retried(match, atts):
    graph = _Once(match, 504)
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_forward(_client(graph), "P1", to=["c@example.com"], cc=None, comment="",
                                attachments=[_att(n, sz) for n, sz in atts])
    hits = [ln for ln in graph.lines() if ln.startswith("POST") and match in ln]
    assert len(hits) == 1


@pytest.mark.parametrize("match,unconfirmed", [
    ("/me/messages", False),   # draft create: nothing sent
    ("/attachments", False),   # attach: nothing sent, draft deleted
    ("/D1/send", True),        # the send itself: outcome unknown
])
def test_only_the_send_call_reports_an_unknown_outcome(match, unconfirmed):
    graph = _Once(match, 503)
    with pytest.raises(OutlookGraphAPIError) as caught:
        graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 2_000_000), _att("b.pdf", 1_000_000)])
    assert isinstance(caught.value, graph_send.SendUnconfirmed) is unconfirmed


def test_a_timeout_on_the_send_call_is_unconfirmed():
    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)
    with pytest.raises(graph_send.SendUnconfirmed) as caught:
        graph_send.send_new(_client(handler), MSG, [_att("a.pdf", 1000)])
    assert caught.value.endpoint == "/me/sendMail"


def test_a_4xx_on_the_send_call_is_a_refusal_not_unconfirmed():
    graph = _Once("/sendMail", 400)
    with pytest.raises(OutlookGraphAPIError) as caught:
        graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 1000)])
    assert not isinstance(caught.value, graph_send.SendUnconfirmed)


def test_a_401_from_the_upload_url_is_not_read_as_our_token_expiring():
    # with_outlook_client refreshes the token on any 401; the upload URL is
    # pre-authenticated, so its 401 says nothing about our token.
    class _Refusing(_Graph):
        def __call__(self, req):
            if req.method == "PUT":
                self.calls.append(req)
                return httpx.Response(401, text="session expired")
            return super().__call__(req)
    graph = _Refusing()
    with pytest.raises(OutlookGraphAPIError) as caught:
        graph_send.send_new(_client(graph), MSG, [_att("big.pdf", 3_500_000)])
    assert caught.value.status_code != 401
    assert caught.value.endpoint == "uploadSession"
    assert "401" in str(caught.value)
    assert graph.lines()[-1] == "DELETE /v1.0/me/messages/D1"


class _ChunkAnswers(_Graph):
    """The first PUT answers each of ``answers`` in turn (a status, or an
    exception to raise), then the session behaves."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def __call__(self, req):
        if req.method == "PUT" and self.answers:
            self.calls.append(req)
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(answer, headers={"Retry-After": "7"} if answer == 429 else {}, text="busy")
        return super().__call__(req)


@pytest.mark.parametrize("answer", [429, 503, 504, httpx.ConnectError("refused")])
def test_a_chunk_put_is_retried_on_the_same_range(answer):
    graph = _ChunkAnswers(answer)
    delays: list[float] = []
    gc = _client(graph)
    gc._sleep = delays.append
    size = 3_500_000
    graph_send.send_new(gc, MSG, [_att("big.pdf", size)])
    puts = [r.headers["Content-Range"] for r in graph.calls if r.method == "PUT"]
    assert puts[0] == puts[1] == f"bytes 0-2949119/{size}"
    assert graph.received["bytes"] == size
    assert graph.lines()[-1] == "POST /v1.0/me/messages/D1/send"
    assert delays == ([7.0] if answer == 429 else [1.0])  # Retry-After honoured


def test_a_chunk_put_that_keeps_timing_out_is_a_graph_error_and_the_draft_goes():
    graph = _ChunkAnswers(*[httpx.ConnectTimeout("down") for _ in range(4)])
    with pytest.raises(OutlookGraphAPIError) as caught:
        graph_send.send_new(_client(graph), MSG, [_att("big.pdf", 3_500_000)])
    assert caught.value.status_code == 504 and caught.value.endpoint == "uploadSession"
    assert not isinstance(caught.value, graph_send.SendUnconfirmed)  # nothing was sent
    assert len([r for r in graph.calls if r.method == "PUT"]) == 4  # first try + max_retries
    assert graph.lines()[-1] == "DELETE /v1.0/me/messages/D1"


def test_a_chunk_put_read_timeout_is_not_retried():
    # Each read timeout already cost the full client timeout; four in a row
    # would outlast the proxy while the worker went on to /send.
    graph = _ChunkAnswers(httpx.ReadTimeout("slow"))
    with pytest.raises(OutlookGraphAPIError) as caught:
        graph_send.send_new(_client(graph), MSG, [_att("big.pdf", 3_500_000)])
    assert caught.value.status_code == 504
    assert len([r for r in graph.calls if r.method == "PUT"]) == 1
    assert graph.lines()[-1] == "DELETE /v1.0/me/messages/D1"


def test_a_chunk_put_still_throttled_after_the_retries_surfaces_the_429():
    graph = _ChunkAnswers(*[429] * 4)
    with pytest.raises(OutlookGraphAPIError) as caught:
        graph_send.send_new(_client(graph), MSG, [_att("big.pdf", 3_500_000)])
    assert caught.value.status_code == 429
    assert "POST /v1.0/me/messages/D1/send" not in graph.lines()


def test_a_504_on_create_upload_session_is_not_retried():
    graph = _Once("/createUploadSession", 504)
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_new(_client(graph), MSG, [_att("big.pdf", 3_500_000)])
    assert len([ln for ln in graph.lines() if ln.endswith("/createUploadSession")]) == 1


def test_a_429_is_still_retried_because_graph_did_nothing():
    graph = _Once("/attachments", 429)
    graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 2_000_000), _att("b.pdf", 1_000_000)])
    attaches = [ln for ln in graph.lines() if ln.endswith("/attachments")]
    assert len(attaches) == 3  # a.pdf throttled then sent, then b.pdf


def test_failed_attach_deletes_the_draft_and_raises():
    graph = _Graph(fail_on="/attachments")
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 2_000_000), _att("b.pdf", 1_000_000)])
    assert graph.lines()[-1] == "DELETE /v1.0/me/messages/D1"
    assert "POST /v1.0/me/messages/D1/send" not in graph.lines()


def test_failed_send_leaves_the_draft_rather_than_risk_the_sent_copy():
    # A failed /send may be one Graph already accepted; deleting the draft id
    # then could take the Sent Items copy.
    graph = _Graph(fail_on="/D1/send")
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 2_000_000), _att("b.pdf", 1_000_000)])
    assert graph.lines()[-1] == "POST /v1.0/me/messages/D1/send"
    assert not [r for r in graph.calls if r.method == "DELETE"]


def test_upload_url_off_microsoft_is_refused_and_nothing_is_put():
    graph = _Graph(upload_url="https://attacker.example/upload")
    with pytest.raises(OutlookGraphAPIError):
        graph_send.send_new(_client(graph), MSG, [_att("big.pdf", 4_000_000)])
    assert not [r for r in graph.calls if r.method == "PUT"]
    assert graph.lines()[-1] == "DELETE /v1.0/me/messages/D1"


@pytest.mark.parametrize("url,ok", [
    (UPLOAD_URL, True),
    ("https://outlook.office365.com/api/x", True),
    ("http://outlook.office.com/api/x", False),
    ("https://outlook.office.com.evil.example/x", False),
    ("https://evil-office.com/x", False),
])
def test_upload_host_check(url, ok):
    assert graph_send._upload_url_ok(url) is ok


def test_large_set_without_sent_items_copy_is_refused(graph):
    with pytest.raises(graph_send.AttachmentsNeedSentItems):
        graph_send.send_new(_client(graph), MSG, [_att("a.pdf", 3_000_000)], save_to_sent_items=False)
    assert graph.calls == []


def test_reply_small_inline_large_via_create_reply(graph):
    graph_send.send_reply(_client(graph), "P1", {"body": {"content": "x"}}, [_att("a.pdf", 10)])
    assert graph.lines() == ["POST /v1.0/me/messages/P1/reply"]
    graph.calls.clear()
    graph_send.send_reply(_client(graph), "P1", {"body": {"content": "x"}}, [_att("a.pdf", 2_600_000)])
    assert graph.lines() == [
        "POST /v1.0/me/messages/P1/createReply",
        "POST /v1.0/me/messages/D1/attachments",
        "POST /v1.0/me/messages/D1/send",
    ]


def test_forward_without_files_is_the_one_call_forward(graph):
    graph_send.send_forward(_client(graph), "P1", to=["x@example.com"], cc=["y@example.com"],
                            comment="fyi", attachments=None)
    assert graph.lines() == ["POST /v1.0/me/messages/P1/forward"]
    body = json.loads(graph.calls[0].content)
    assert body["message"]["ccRecipients"][0]["emailAddress"]["address"] == "y@example.com"


def test_forward_with_any_added_file_uses_create_forward(graph):
    graph_send.send_forward(_client(graph), "P1", to=["x@example.com"], cc=None,
                            comment="see attached", attachments=[_att("a.pdf", 10)])
    assert graph.lines() == [
        "POST /v1.0/me/messages/P1/createForward",
        "POST /v1.0/me/messages/D1/attachments",
        "POST /v1.0/me/messages/D1/send",
    ]
    body = json.loads(graph.calls[0].content)
    assert body["comment"] == "see attached"
    assert body["toRecipients"][0]["emailAddress"]["address"] == "x@example.com"
