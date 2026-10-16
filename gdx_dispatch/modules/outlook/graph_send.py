"""Send mail through Graph with attachments of any size up to the email cap.

``/me/sendMail`` and ``/reply`` carry attachments inline, and Graph caps the
whole request at 4 MB — base64 included, so roughly 3 MB of real files. A
phone photo or two, or two estimate PDFs with photos, is past that. Above the
inline budget this module takes the route Microsoft documents for big files:

1. create a draft — ``POST /me/messages`` (new mail),
   ``/me/messages/{id}/createReply`` or ``/createForward``;
2. attach each file — ``POST /me/messages/{draft}/attachments`` under 3 MB,
   otherwise ``createUploadSession`` and byte-range PUTs to the session's
   pre-authenticated ``uploadUrl`` (no Authorization header; chunks < 4 MB);
3. ``POST /me/messages/{draft}/send``.

If anything fails after the draft exists, the draft is deleted so a failed
send does not leave a half-built message in the sender's Drafts folder.

Read 2026-10-15:
- https://learn.microsoft.com/en-us/graph/outlook-large-attachments (updated 2025-08-06)
- https://learn.microsoft.com/en-us/graph/api/attachment-createuploadsession
- https://learn.microsoft.com/en-us/graph/api/message-createforward
- https://learn.microsoft.com/en-us/graph/api/message-forward (updated 2025-07-23):
  ``/forward`` takes only ``comment``, ``toRecipients`` and ``message`` — no
  added files — so a forward with extra files always goes through a draft.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from gdx_dispatch.modules.outlook.graph_client import RETRY_THROTTLED_ONLY, OutlookGraphAPIError

log = logging.getLogger("gdx_dispatch.modules.outlook.graph_send")

# Raw bytes of attachments that may ride inline in one sendMail / reply
# request. Matches transactional_email.MAX_INLINE_ATTACHMENT_BYTES: 2.5 MB raw
# is ~3.3 MB of base64, leaving the body room under Graph's 4 MB request cap.
INLINE_TOTAL_BYTES = 2_500_000

# Per-file threshold for POST /attachments; above it, an upload session.
DIRECT_ATTACH_BYTES = 3_000_000

# Upload-session chunk: under the documented 4 MB per PUT, and a multiple of
# 320 KiB, the granularity Microsoft's upload sessions are built around.
UPLOAD_CHUNK_BYTES = 9 * 320 * 1024

# The uploadUrl is pre-authenticated and gets PUT without our token; still,
# only ever send customer files to a Microsoft mail host.
_UPLOAD_HOST_SUFFIXES = (".office.com", ".office365.com")


class AttachmentsNeedSentItems(ValueError):
    """Attachments too big for one inline request, on a send that asked not
    to be saved to Sent Items. The draft route always lands in Sent Items, so
    it is refused instead of quietly keeping a copy the caller ruled out."""


class SendUnconfirmed(OutlookGraphAPIError):
    """The call that sends the mail failed without saying whether it sent it:
    a 5xx, a timeout or a dropped connection. The mail may have gone. Every
    earlier step (token refresh, draft create, attach) fails with nothing sent
    and raises the plain error instead."""


def _send_call(gc: Any, path: str, payload: dict[str, Any] | None = None) -> None:
    """The POST that sends the mail. Not retried past a 429 (see
    ``RETRY_THROTTLED_ONLY``); an unknown outcome is raised as
    :class:`SendUnconfirmed`."""
    try:
        gc._request("POST", path, json=payload, retry_on=RETRY_THROTTLED_ONLY)
    except OutlookGraphAPIError as exc:
        if exc.status_code >= 500:
            raise SendUnconfirmed(exc.status_code, exc.body, endpoint=path) from exc
        raise
    except httpx.HTTPError as exc:
        raise SendUnconfirmed(504, repr(exc), endpoint=path) from exc


def _size(att: dict[str, Any]) -> int:
    if att.get("size_bytes") is not None:
        return int(att["size_bytes"])
    return len(base64.b64decode(att.get("content_base64") or ""))


def total_bytes(attachments: list[dict[str, Any]] | None) -> int:
    return sum(_size(a) for a in attachments or ())


def file_attachment(att: dict[str, Any]) -> dict[str, Any]:
    """Our wire dict → Graph fileAttachment."""
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": att.get("name") or "attachment",
        "contentType": att.get("content_type") or "application/octet-stream",
        "contentBytes": att["content_base64"],
    }


def fits_inline(attachments: list[dict[str, Any]] | None,
                message: dict[str, Any] | None = None) -> bool:
    """Whether the set may ride inline. The message counts against the same
    budget: Graph's 4 MB cap is on the whole request, and a long quoted body
    with 2.5 MB of files would otherwise be refused outright."""
    body = len(json.dumps(message).encode()) if message else 0
    return total_bytes(attachments) + body <= INLINE_TOTAL_BYTES


def _upload_url_ok(url: str) -> bool:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == s[1:] or host.endswith(s) for s in _UPLOAD_HOST_SUFFIXES
    )


def _attach(gc: Any, draft_id: str, att: dict[str, Any]) -> None:
    data = base64.b64decode(att["content_base64"])
    if len(data) < DIRECT_ATTACH_BYTES:
        gc._request("POST", f"/me/messages/{draft_id}/attachments", json=file_attachment(att),
                    retry_on=RETRY_THROTTLED_ONLY)
        return
    endpoint = f"/me/messages/{draft_id}/attachments/createUploadSession"
    resp = gc._request(
        "POST",
        endpoint,
        json={
            "AttachmentItem": {
                "attachmentType": "file",
                "name": att.get("name") or "attachment",
                "size": len(data),
                "contentType": att.get("content_type") or "application/octet-stream",
            }
        },
    )
    upload_url = (resp.json() or {}).get("uploadUrl") or ""
    if not _upload_url_ok(upload_url):
        raise OutlookGraphAPIError(502, "upload session returned an unexpected host", endpoint=endpoint)
    total = len(data)
    start = 0
    while start < total:
        chunk = data[start:start + UPLOAD_CHUNK_BYTES]
        end = start + len(chunk) - 1
        put = gc._client.put(
            upload_url,
            content=chunk,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
        )
        if put.status_code >= 400:
            # The uploadUrl carries its own authorization, not our token: a
            # 401 here is the session refusing, not our token expiring, and
            # must not send with_outlook_client off to refresh a good token.
            status = 502 if put.status_code == 401 else put.status_code
            raise OutlookGraphAPIError(status, f"upload session answered {put.status_code}: {put.text[:500]}",
                                       endpoint="uploadSession")
        start = end + 1


def _send_draft(gc: Any, create_path: str, create_body: dict[str, Any] | None,
                attachments: list[dict[str, Any]]) -> None:
    # Every POST below creates or sends something, so none is retried past a
    # 429: a 504 after Graph did the work would attach a file twice, leave an
    # orphan draft, or send the mail twice.
    resp = gc._request("POST", create_path, json=create_body if create_body is not None else {},
                       retry_on=RETRY_THROTTLED_ONLY)
    draft_id = (resp.json() or {}).get("id")
    if not draft_id:
        raise OutlookGraphAPIError(502, "draft create returned no id", endpoint=create_path)
    try:
        for att in attachments:
            _attach(gc, draft_id, att)
    except Exception:
        try:
            gc._request("DELETE", f"/me/messages/{draft_id}")
        except Exception:  # noqa: BLE001 — the attach error is the one to surface
            log.warning("graph_send: could not delete failed draft %s", draft_id, exc_info=True)
        raise
    # Not deleted when /send itself fails: a timeout there may be a send Graph
    # already accepted, and the delete would then take the Sent Items copy. A
    # draft left in Drafts is visible and harmless; a vanished sent copy is not.
    _send_call(gc, f"/me/messages/{draft_id}/send")


def _with_inline(message: dict[str, Any], atts: list[dict[str, Any]]) -> dict[str, Any]:
    """``message`` carrying ``atts`` as fileAttachments, for the one-call routes."""
    msg = dict(message)
    if atts:
        msg["attachments"] = [file_attachment(a) for a in atts]
    return msg


def send_new(gc: Any, message: dict[str, Any], attachments: list[dict[str, Any]] | None,
             *, save_to_sent_items: bool = True) -> str:
    """Send a new message. ``message`` is the Graph message object without
    attachments. Returns ``"inline"`` or ``"draft"`` (the route taken)."""
    atts = list(attachments or ())
    if fits_inline(atts, message):
        _send_call(gc, "/me/sendMail",
                   {"message": _with_inline(message, atts), "saveToSentItems": save_to_sent_items})
        return "inline"
    if not save_to_sent_items:
        raise AttachmentsNeedSentItems(
            "attachments over the inline limit can only be sent with a Sent Items copy"
        )
    _send_draft(gc, "/me/messages", dict(message), atts)
    return "draft"


def send_reply(gc: Any, parent_graph_id: str, message: dict[str, Any],
               attachments: list[dict[str, Any]] | None) -> str:
    """Reply on the parent's thread (Graph sets In-Reply-To / References)."""
    atts = list(attachments or ())
    if fits_inline(atts, message):
        _send_call(gc, f"/me/messages/{parent_graph_id}/reply", {"message": _with_inline(message, atts)})
        return "inline"
    _send_draft(gc, f"/me/messages/{parent_graph_id}/createReply", {"message": dict(message)}, atts)
    return "draft"


def send_forward(gc: Any, parent_graph_id: str, *, to: list[str], cc: list[str] | None,
                 comment: str, attachments: list[dict[str, Any]] | None) -> str:
    """Forward the parent (its own attachments ride along), plus any added
    files. Graph's one-call ``/forward`` cannot add files, so added files
    always take the draft route regardless of size."""
    body: dict[str, Any] = {
        "comment": comment or "",
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }
    if cc:
        # Neither forward action has a ccRecipients field; it rides on message.
        body["message"] = {"ccRecipients": [{"emailAddress": {"address": a}} for a in cc]}
    atts = list(attachments or ())
    if not atts:
        _send_call(gc, f"/me/messages/{parent_graph_id}/forward", body)
        return "inline"
    _send_draft(gc, f"/me/messages/{parent_graph_id}/createForward", body, atts)
    return "draft"
