"""A Graph call that creates, sends, moves or copies is retried only on 429.

Graph refuses a throttled call before doing any of it; a 503/504 can follow
work already done, and a retry would then send the mail twice, copy it twice
or leave a second draft or subscription. The default of ``_request`` follows
the method, and every POST site also says so explicitly — the scan below
fails the first one that does not (GDXA-1318).
"""
from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from gdx_dispatch.modules.outlook.graph_client import (
    OutlookGraphAPIError,
    OutlookGraphClient,
)

ROOT = Path(__file__).resolve().parents[1]
SCANNED = sorted((ROOT / "modules" / "outlook").rglob("*.py")) + [ROOT / "core" / "transactional_email.py"]


class _Answers:
    def __init__(self, status: int):
        self.status = status
        self.calls: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if len(self.calls) == 1:
            return httpx.Response(self.status, json={"error": {"code": "x"}})
        return httpx.Response(200, json={"id": "M2"})


def _client(handler) -> OutlookGraphClient:
    gc = OutlookGraphClient("tok", sleep=lambda _s: None)
    gc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gc


@pytest.mark.parametrize("status", [503, 504])
def test_a_post_left_on_the_default_is_not_retried_on_5xx(status):
    graph = _Answers(status)
    with pytest.raises(OutlookGraphAPIError):
        _client(graph)._request("POST", "/me/messages/M1/copy", json={"destinationId": "F"})
    assert len(graph.calls) == 1


def test_a_post_left_on_the_default_is_still_retried_on_429():
    graph = _Answers(429)
    _client(graph)._request("POST", "/me/sendMail", json={})
    assert len(graph.calls) == 2


@pytest.mark.parametrize("method", ["GET", "DELETE", "PATCH", "get"])
def test_a_repeatable_method_keeps_the_transient_retry(method):
    graph = _Answers(503)
    _client(graph)._request(method, "/me/messages/M1", json={"isRead": True} if method == "PATCH" else None)
    assert len(graph.calls) == 2


@pytest.mark.parametrize("call", [
    lambda gc: gc.create_folder(display_name="Jobs"),
    lambda gc: gc.move_folder("F1", dest_parent_id="F2"),
    lambda gc: gc.move_message("M1", dest_folder_id="F2"),
    lambda gc: gc.copy_message("M1", dest_folder_id="F2"),
])
def test_folder_and_message_actions_are_not_retried_on_504(call):
    graph = _Answers(504)
    with pytest.raises(OutlookGraphAPIError):
        call(_client(graph))
    assert len(graph.calls) == 1


def _calls(attr: str):
    for path in SCANNED:
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr:
                yield str(path.relative_to(ROOT)), node


def _post_sites() -> list[tuple[str, int, bool]]:
    """(file, line, passes retry_on) for every ``*._request`` whose method is
    a literal POST, positional or ``method=``. A method held in a variable
    is not seen here; the method-based default still covers it."""
    out = []
    for rel, node in _calls("_request"):
        method = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "method"), None)
        if isinstance(method, ast.Constant) and str(method.value).upper() == "POST":
            passes = any(k.arg == "retry_on" for k in node.keywords)
            out.append((rel, node.lineno, passes))
    return out


def test_no_raw_client_post_put_patch_except_the_upload_chunk():
    # Sees ``_client.post/put/patch`` only, not ``_client.request`` or an
    # aliased client. ``_client.put`` in graph_send._put_chunk is the one raw write: the
    # pre-authenticated uploadUrl must not carry our token, and it carries
    # its own retry (test_outlook_graph_send).
    raw = [
        f"{rel}:{node.lineno}"
        for verb in ("post", "put", "patch")
        for rel, node in _calls(verb)
        if isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "_client"
    ]
    assert raw == ["modules/outlook/graph_send.py:" + str(_put_chunk_line())], raw


def _put_chunk_line() -> int:
    src = (ROOT / "modules" / "outlook" / "graph_send.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_put_chunk")
    return next(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "put")


def test_every_graph_post_says_how_it_is_retried():
    sites = _post_sites()
    # The scan must be able to see the sites it guards: graph_send's send
    # call, draft create, attach and upload session; the draft endpoint;
    # subscriptions; four folder/message actions. Transactional sendMail has
    # no site of its own: it goes through graph_send's send call
    # (test_transactional_email_attachments pins its retry_on).
    assert len(sites) >= 10, sites
    bare = [f"{f}:{ln}" for f, ln, passes in sites if not passes]
    assert bare == [], f"Graph POST on the default retry: {bare}"
