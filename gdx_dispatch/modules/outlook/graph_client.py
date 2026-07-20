"""Sprint Outlook Integration — Microsoft Graph httpx wrapper.

Read-only methods this slice ships:
  - validate_token(): GET /me — confirms the access token is valid + returns user metadata
  - get_mailbox_settings(): GET /me/mailboxSettings — timezone + signature
  - list_messages(folder, top, skip, delta_token): GET /me/mailFolders/{folder}/messages or /messages/delta
  - get_message(message_id): GET /me/messages/{id}
  - download_attachment(message_id, attachment_id): GET /me/messages/{id}/attachments/{aid}/$value (binary)

Send / subscribe / draft methods land in slices S12, S31.
Token refresh is the caller's responsibility (slice S9 helper).

Microsoft Graph reference:
- Authoritative endpoint table: https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview
- All endpoints documented inline below for offline implementer.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("gdx_dispatch.modules.outlook.graph_client")

# Graph throttles with 429 (+ a Retry-After header) and occasionally returns
# transient 503/504. These are retried with backoff; everything else raises.
_RETRYABLE_STATUS = frozenset({429, 503, 504})


class OutlookGraphAPIError(RuntimeError):
    """Raised on non-2xx Graph response. Carries status_code + body for diagnosis."""

    def __init__(self, status_code: int, body: Any, *, endpoint: str | None = None):
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint
        super().__init__(
            f"Microsoft Graph {endpoint or ''} returned {status_code}: {body}"
        )


@dataclass(frozen=True)
class MailboxIdentity:
    """Result of validate_token()."""
    upn: str
    display_name: str | None
    user_id: str  # Microsoft's user GUID — NOT GDX's user_id
    raw: dict[str, Any]


class OutlookGraphClient:
    """Thin sync wrapper over a subset of Microsoft Graph endpoints."""

    def __init__(
        self,
        access_token: str,
        *,
        timeout_s: int = 30,
        base_url: str = "https://graph.microsoft.com/v1.0",
        max_retries: int = 3,
        retry_max_delay_s: float = 60.0,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token is required")
        self._access_token = access_token
        self._timeout_s = timeout_s
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_s)
        self._max_retries = max_retries
        self._retry_max_delay_s = retry_max_delay_s
        self._sleep = sleep or time.sleep  # injectable for tests

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OutlookGraphClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── core HTTP ───────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        accept_binary: bool = False,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/octet-stream" if accept_binary else "application/json",
        }
        if json is not None:
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            resp = self._client.request(method, url, headers=headers, params=params, json=json)
            if resp.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                delay = self._retry_delay(resp, attempt)
                log.warning(
                    "graph_throttled status=%s endpoint=%s retry=%d/%d after=%.1fs",
                    resp.status_code, path, attempt + 1, self._max_retries, delay,
                )
                self._sleep(delay)
                attempt += 1
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except Exception:  # noqa: BLE001
                    body = resp.text
                raise OutlookGraphAPIError(resp.status_code, body, endpoint=path)
            return resp

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        """Seconds to wait before retrying a throttled/transient response.
        Honors a numeric ``Retry-After`` header (Graph sends seconds); otherwise
        exponential backoff (1, 2, 4, …). Always capped at ``retry_max_delay_s``."""
        retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), self._retry_max_delay_s)
            except ValueError:
                pass  # HTTP-date form — fall through to backoff
        return min(2.0 ** attempt, self._retry_max_delay_s)

    # ── auth + metadata ─────────────────────────────────────────────────

    def validate_token(self) -> MailboxIdentity:
        """Hit GET /me. Returns identity on 200; raises OutlookGraphAPIError on 401/403/etc."""
        resp = self._request("GET", "/me")
        body = resp.json()
        return MailboxIdentity(
            upn=body.get("userPrincipalName") or body.get("mail") or "",
            display_name=body.get("displayName"),
            user_id=body.get("id") or "",
            raw=body,
        )

    def get_mailbox_settings(self) -> dict[str, Any]:
        """GET /me/mailboxSettings — timezone + automaticRepliesSetting + language."""
        return self._request("GET", "/me/mailboxSettings").json()

    # ── messages ────────────────────────────────────────────────────────

    # Microsoft Graph v1.0 has no `inReplyTo` property on
    # microsoft.graph.message — it lives only inside internetMessageHeaders
    # (a non-selectable expanded property). Including it in $select fails
    # with HTTP 400 RequestBroker--ParseUri on every message-list call.
    # Threading is reconstructed via conversationId (already selected).
    _DEFAULT_SELECT = (
        "id,internetMessageId,conversationId,subject,from,toRecipients,"
        "ccRecipients,bccRecipients,sentDateTime,receivedDateTime,bodyPreview,"
        "hasAttachments,isRead"
    )

    def list_messages(
        self,
        *,
        folder: str = "Inbox",
        top: int = 50,
        skip: int = 0,
        delta_token: str | None = None,
        select: str | None = None,
    ) -> dict[str, Any]:
        """List messages in a folder, or pull a delta if delta_token is given.

        When delta_token is provided, the path becomes /me/mailFolders/{folder}/messages/delta
        with the deltaToken query — Graph returns only changes since the last delta.

        NOTE: without a token this hits the PLAIN listing, which never
        returns an @odata.deltaLink — fine for interactive views, wrong
        for sync. Sync callers must use list_messages_delta() so the
        first pass can bootstrap a token.
        """
        if delta_token:
            path = f"/me/mailFolders/{folder}/messages/delta"
            params = {"$deltatoken": delta_token}
        else:
            path = f"/me/mailFolders/{folder}/messages"
            params = {"$top": top, "$skip": skip,
                      "$orderby": "receivedDateTime desc",
                      "$select": select or self._DEFAULT_SELECT}
        return self._request("GET", path, params=params).json()

    def list_messages_delta(
        self,
        *,
        folder: str,
        delta_token: str | None = None,
        top: int = 100,
        select: str | None = None,
    ) -> dict[str, Any]:
        """Delta-list a folder — ALWAYS via /messages/delta.

        2026-07-07 audit: the sync path called list_messages(), whose
        token-less branch hits the plain listing; Graph never returned a
        deltaLink, so no folder ever saved a token and every 30-minute
        fallback poll re-upserted the entire mailbox. The delta endpoint
        with no token walks the folder once (paged via nextLink) and ends
        with the deltaLink the next run resumes from.

        Delta doesn't accept $orderby/$skip; $top and $select apply to the
        initial request only (Graph carries them through the nextLink).
        """
        path = f"/me/mailFolders/{folder}/messages/delta"
        if delta_token:
            params: dict[str, Any] = {"$deltatoken": delta_token}
        else:
            params = {"$top": top, "$select": select or self._DEFAULT_SELECT}
        return self._request("GET", path, params=params).json()

    def get_message(self, message_id: str) -> dict[str, Any]:
        """GET /me/messages/{id} — full message including body.content."""
        return self._request(
            "GET", f"/me/messages/{message_id}",
            params={"$select": self._DEFAULT_SELECT + ",body"},
        ).json()

    # ── folders ─────────────────────────────────────────────────────────

    # NOTE: wellKnownName is a beta-only property; including it in v1.0
    # $select fails with 400 BadRequest. We resolve well-known names by
    # querying each alias separately (resolve_well_known_folder_ids).
    _FOLDER_SELECT = (
        "id,displayName,parentFolderId,childFolderCount,unreadItemCount,"
        "totalItemCount,isHidden"
    )

    WELL_KNOWN_FOLDER_NAMES = (
        "inbox",
        "sentitems",
        "drafts",
        "deleteditems",
        "junkemail",
        "outbox",
        "archive",
    )

    def list_folders(
        self,
        *,
        parent_id: str | None = None,
        include_hidden: bool = False,
        top: int = 100,
    ) -> list[dict[str, Any]]:
        """List child folders under a parent (None = top-level). Single page;
        caller paginates via @odata.nextLink if needed.

        wellKnownName is a beta-ish hint Microsoft surfaces on system
        folders ("inbox", "sentitems", "drafts", "deleteditems", "junkemail",
        "archive", "outbox"). Custom folders return None.
        """
        if parent_id:
            path = f"/me/mailFolders/{parent_id}/childFolders"
        else:
            path = "/me/mailFolders"
        params: dict[str, Any] = {"$top": top, "$select": self._FOLDER_SELECT}
        if include_hidden:
            params["includeHiddenFolders"] = "true"
        return self._request("GET", path, params=params).json().get("value", [])

    def list_all_folders(
        self,
        *,
        max_depth: int = 5,
        include_hidden: bool = False,
        max_total: int = 500,
    ) -> list[dict[str, Any]]:
        """Recursive folder walk capped at max_depth + max_total folders.

        Returns flat list with synthetic 'depth' key + 'wellKnownName' key
        merged in (resolved by separate alias query — Graph v1.0 does not
        $select wellKnownName).
        """
        wk_map = self.resolve_well_known_folder_ids()
        result: list[dict[str, Any]] = []
        stack: list[tuple[str | None, int]] = [(None, 0)]
        while stack and len(result) < max_total:
            parent_id, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                folders = self.list_folders(parent_id=parent_id, include_hidden=include_hidden)
            except OutlookGraphAPIError as exc:
                log.warning("list_all_folders: failed to list under parent=%s: %s", parent_id, exc)
                continue
            for f in folders:
                if len(result) >= max_total:
                    break
                f["depth"] = depth
                f["wellKnownName"] = wk_map.get(f.get("id"))
                result.append(f)
                if f.get("childFolderCount", 0) > 0 and depth < max_depth:
                    stack.append((f["id"], depth + 1))
        return result

    def resolve_well_known_folder_ids(self) -> dict[str, str]:
        """Build {folder_id: well_known_name} map by querying each alias.

        Microsoft Graph v1.0 doesn't expose `wellKnownName` on the
        mailFolder resource, but `/me/mailFolders/{alias}` resolves the
        alias to a real folder. Walk the known set, collect ids, return
        the inverse map. Failures (e.g., archive disabled on the tenant)
        are non-fatal — that name simply doesn't appear in the result.
        """
        out: dict[str, str] = {}
        for name in self.WELL_KNOWN_FOLDER_NAMES:
            try:
                resp = self._request("GET", f"/me/mailFolders/{name}", params={"$select": "id"})
                folder_id = resp.json().get("id")
                if folder_id:
                    out[folder_id] = name
            except OutlookGraphAPIError as exc:
                log.debug("resolve_well_known_folder_ids: %s not present (%s)", name, exc.status_code)
        return out

    def create_folder(
        self,
        *,
        display_name: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a folder. parent_id=None creates at top level."""
        path = f"/me/mailFolders/{parent_id}/childFolders" if parent_id else "/me/mailFolders"
        body = {"displayName": display_name, "isHidden": False}
        return self._request("POST", path, json=body).json()

    def rename_folder(self, folder_id: str, *, display_name: str) -> dict[str, Any]:
        """Rename a folder. Only displayName is mutable on a mailFolder."""
        return self._request(
            "PATCH",
            f"/me/mailFolders/{folder_id}",
            json={"displayName": display_name},
        ).json()

    def delete_folder(self, folder_id: str) -> None:
        """Soft-delete a folder (Graph moves it to Recoverable Items for ~30d).
        Cannot delete well-known folders — Microsoft returns 400."""
        self._request("DELETE", f"/me/mailFolders/{folder_id}")

    def move_folder(self, folder_id: str, *, dest_parent_id: str) -> dict[str, Any]:
        """Move a folder under a new parent."""
        return self._request(
            "POST",
            f"/me/mailFolders/{folder_id}/move",
            json={"destinationId": dest_parent_id},
        ).json()

    def empty_folder(self, folder_id: str) -> None:
        """Delete every message in a folder. Iterates pages of message ids
        and DELETEs each. Graph has no native 'empty folder' endpoint."""
        while True:
            page = self._request(
                "GET",
                f"/me/mailFolders/{folder_id}/messages",
                params={"$select": "id", "$top": 100},
            ).json()
            ids = [m["id"] for m in page.get("value", [])]
            if not ids:
                return
            for mid in ids:
                try:
                    self._request("DELETE", f"/me/messages/{mid}")
                except OutlookGraphAPIError as exc:
                    log.warning("empty_folder: failed to delete %s: %s", mid, exc)

    # ── messages: move/copy ─────────────────────────────────────────────

    def move_message(self, message_id: str, *, dest_folder_id: str) -> dict[str, Any]:
        """Move message to another folder. NOTE: Graph creates a new message
        at the destination with a NEW id; the source id is invalidated. Use
        the returned id (or internetMessageId as a stable cross-folder key)."""
        return self._request(
            "POST",
            f"/me/messages/{message_id}/move",
            json={"destinationId": dest_folder_id},
        ).json()

    def copy_message(self, message_id: str, *, dest_folder_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/me/messages/{message_id}/copy",
            json={"destinationId": dest_folder_id},
        ).json()

    def mark_message_read(self, message_id: str, *, is_read: bool = True) -> None:
        self._request("PATCH", f"/me/messages/{message_id}", json={"isRead": is_read})

    # ── attachments ─────────────────────────────────────────────────────

    def list_attachments(self, message_id: str) -> list[dict[str, Any]]:
        # $select the METADATA only. Without it, Graph returns `contentBytes`
        # (full base64) for every fileAttachment inline, so a plain list of a
        # message with big attachments buffers them all into memory + JSON on
        # open. Bytes are fetched on demand via download_attachment(/$value).
        # No caller reads contentBytes off this listing (D4 downloads lazily;
        # vendor_bill_ingest also downloads separately).
        body = self._request(
            "GET",
            f"/me/messages/{message_id}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
        ).json()
        return body.get("value", [])

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Returns raw attachment bytes. For inline images use this; persist to R2."""
        resp = self._request(
            "GET",
            f"/me/messages/{message_id}/attachments/{attachment_id}/$value",
            accept_binary=True,
        )
        return resp.content
