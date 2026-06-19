"""Folder management endpoints — CRUD on Microsoft Graph mailFolders +
GDX-side folder preferences (color, icon, pinned, sort_order).

Routes are mounted under /api/outlook (same as views_router):

    GET    /folders                       — flat list with prefs joined
    POST   /folders                       — create (Graph + cache refresh)
    PATCH  /folders/{id}                  — rename in Graph and/or update prefs
    DELETE /folders/{id}                  — delete in Graph + cascade local
    POST   /folders/{id}/empty            — delete every message in folder
    POST   /folders/{id}/mark-all-read    — mark all messages read
    POST   /messages/{id}/move            — move message to another folder

Auth: every endpoint requires `email` module + an authed user. Folder
mutations also require admin role (no tech should rename / delete shared
folders without admin authorization).
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.models import (
    OutlookAccount,
    OutlookFolder,
    OutlookFolderPrefs,
    OutlookMessage,
    OutlookFolderSyncState,
)
from gdx_dispatch.modules.outlook.token_refresh import OutlookReconnectRequired, with_outlook_client
from gdx_dispatch.routers.auth import get_current_user


log = logging.getLogger("gdx_dispatch.modules.outlook.folders_router")

router = APIRouter(
    prefix="/api/outlook",
    tags=["outlook", "folders"],
)


# ── pydantic shapes ────────────────────────────────────────────────────


class FolderOut(BaseModel):
    id: UUID
    graph_folder_id: str
    display_name: str
    parent_folder_id: str | None = None
    well_known_name: str | None = None
    total_count: int
    unread_count: int
    child_folder_count: int
    is_hidden: bool
    depth: int
    is_system: bool
    # Prefs
    color: str | None = None
    icon: str | None = None
    pinned: bool = False
    sort_order: int = 0


class FolderCreateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=500)
    parent_folder_id: str | None = None


class FolderPatchIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=500)
    color: str | None = Field(default=None, max_length=32)
    icon: str | None = Field(default=None, max_length=64)
    pinned: bool | None = None
    sort_order: int | None = None


class MessageMoveIn(BaseModel):
    destination_folder_id: str = Field(min_length=1, max_length=255)


class MessageReadIn(BaseModel):
    is_read: bool


class GenericOk(BaseModel):
    ok: bool = True
    detail: str | None = None


# ── helpers ────────────────────────────────────────────────────────────


SYSTEM_WELL_KNOWN = {
    "inbox", "drafts", "sentitems", "deleteditems", "junkemail",
    "outbox", "archive",
}


def _user_id(user: dict[str, Any]) -> UUID:
    raw = user.get("user_id") or user.get("id") or user.get("sub")
    if raw is None:
        raise HTTPException(status_code=400, detail="missing user_id")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _tenant_id(user: dict[str, Any]) -> UUID:
    raw = user.get("tenant_id")
    if raw is None:
        raise HTTPException(status_code=400, detail="missing tenant_id")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _account_for_user(tenant_db: Session, user_id: UUID) -> OutlookAccount:
    acct = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id))
        .one_or_none()
    )
    if acct is None:
        raise HTTPException(status_code=404, detail="no outlook account for current user")
    return acct


def _is_admin(user: dict[str, Any]) -> bool:
    return (user.get("role") or "").lower() in {"admin", "owner"}


def _require_admin(user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="admin role required")


def _to_folder_out(folder: OutlookFolder, prefs: OutlookFolderPrefs | None) -> FolderOut:
    return FolderOut(
        id=folder.id,
        graph_folder_id=folder.graph_folder_id,
        display_name=folder.display_name,
        parent_folder_id=folder.parent_folder_id,
        well_known_name=folder.well_known_name,
        total_count=folder.total_count,
        unread_count=folder.unread_count,
        child_folder_count=folder.child_folder_count,
        is_hidden=folder.is_hidden,
        depth=folder.depth,
        is_system=(folder.well_known_name or "") in SYSTEM_WELL_KNOWN,
        color=prefs.color if prefs else None,
        icon=prefs.icon if prefs else None,
        pinned=prefs.pinned if prefs else False,
        sort_order=prefs.sort_order if prefs else 0,
    )


def _get_or_create_prefs(
    tenant_db: Session, account_id: UUID, folder_id: str,
) -> OutlookFolderPrefs:
    prefs = (
        tenant_db.query(OutlookFolderPrefs)
        .filter(
            OutlookFolderPrefs.account_id == account_id,
            OutlookFolderPrefs.folder_id == folder_id,
        )
        .one_or_none()
    )
    if prefs is None:
        prefs = OutlookFolderPrefs()
        prefs.account_id = account_id
        prefs.folder_id = folder_id
        tenant_db.add(prefs)
        tenant_db.flush()
    return prefs


# ── endpoints ──────────────────────────────────────────────────────────


@router.get(
    "/folders",
    response_model=list[FolderOut],
    dependencies=[Depends(require_module("email"))],
)
def list_folders_route(
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
) -> list[FolderOut]:
    """Flat folder list with per-user preferences merged in. UI expands
    into a tree using parent_folder_id + depth.

    2026-04-29: when no OutlookAccount is wired for the current user we
    now return an empty list (200) instead of 404. The /inbox view treats
    a 404 as a hard error and the user has no way to recover — empty list
    + the "Connect Outlook" CTA in Settings is the right empty-state path."""
    uid = _user_id(user)
    acct = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(uid))
        .one_or_none()
    )
    if acct is None:
        return []
    folders = (
        tenant_db.query(OutlookFolder)
        .filter(OutlookFolder.account_id == acct.id)
        .order_by(OutlookFolder.depth, OutlookFolder.display_name)
        .all()
    )
    prefs_map = {
        p.folder_id: p
        for p in tenant_db.query(OutlookFolderPrefs).filter(
            OutlookFolderPrefs.account_id == acct.id,
        ).all()
    }
    return [_to_folder_out(f, prefs_map.get(f.graph_folder_id)) for f in folders]


@router.post(
    "/folders",
    response_model=FolderOut,
    dependencies=[Depends(require_module("email"))],
)
def create_folder_route(
    payload: FolderCreateIn,
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> FolderOut:
    """Create a folder via Graph, then upsert local cache row."""
    _require_admin(user)
    uid = _user_id(user)
    tid = _tenant_id(user)
    account = _account_for_user(tenant_db, uid)
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            created = gc.create_folder(
                display_name=payload.display_name,
                parent_id=payload.parent_folder_id,
            )
    except OutlookReconnectRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OutlookGraphAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Graph error: {exc.status_code}") from exc

    row = OutlookFolder()
    row.account_id = account.id
    row.graph_folder_id = created["id"]
    row.display_name = created.get("displayName") or payload.display_name
    row.parent_folder_id = created.get("parentFolderId") or payload.parent_folder_id
    row.well_known_name = (created.get("wellKnownName") or None) and str(created["wellKnownName"]).lower()
    row.total_count = int(created.get("totalItemCount") or 0)
    row.unread_count = int(created.get("unreadItemCount") or 0)
    row.child_folder_count = int(created.get("childFolderCount") or 0)
    row.is_hidden = bool(created.get("isHidden"))
    # Compute depth by walking up parent chain in our cache.
    parent_chain_depth = 0
    parent_id = row.parent_folder_id
    while parent_id and parent_chain_depth < 10:
        parent_row = (
            tenant_db.query(OutlookFolder)
            .filter(
                OutlookFolder.account_id == account.id,
                OutlookFolder.graph_folder_id == parent_id,
            )
            .one_or_none()
        )
        if parent_row is None:
            break
        parent_chain_depth = parent_row.depth + 1
        break  # only compute one level above; sync will normalize the rest
    row.depth = parent_chain_depth
    tenant_db.add(row)
    tenant_db.commit()
    tenant_db.refresh(row)
    return _to_folder_out(row, None)


@router.patch(
    "/folders/{folder_id}",
    response_model=FolderOut,
    dependencies=[Depends(require_module("email"))],
)
def patch_folder_route(
    folder_id: str,
    payload: FolderPatchIn,
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> FolderOut:
    """Rename in Graph (if display_name set) AND/OR update local prefs
    (color/icon/pinned/sort_order)."""
    uid = _user_id(user)
    tid = _tenant_id(user)
    account = _account_for_user(tenant_db, uid)
    folder = (
        tenant_db.query(OutlookFolder)
        .filter(
            OutlookFolder.account_id == account.id,
            OutlookFolder.graph_folder_id == folder_id,
        )
        .one_or_none()
    )
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")

    # Rename requires admin (mutates Microsoft side, visible to all clients).
    if payload.display_name is not None:
        _require_admin(user)
        if (folder.well_known_name or "") in SYSTEM_WELL_KNOWN:
            raise HTTPException(status_code=400, detail="cannot rename a system folder")
        try:
            with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
                gc.rename_folder(folder_id, display_name=payload.display_name)
        except OutlookReconnectRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OutlookGraphAPIError as exc:
            raise HTTPException(status_code=502, detail=f"Graph error: {exc.status_code}") from exc
        folder.display_name = payload.display_name

    # Prefs are per-user; any authed user with email module can change their own.
    if any(v is not None for v in (payload.color, payload.icon, payload.pinned, payload.sort_order)):
        prefs = _get_or_create_prefs(tenant_db, account.id, folder_id)
        if payload.color is not None:
            prefs.color = payload.color or None
        if payload.icon is not None:
            prefs.icon = payload.icon or None
        if payload.pinned is not None:
            prefs.pinned = payload.pinned
        if payload.sort_order is not None:
            prefs.sort_order = payload.sort_order

    tenant_db.commit()
    tenant_db.refresh(folder)
    prefs = (
        tenant_db.query(OutlookFolderPrefs)
        .filter(
            OutlookFolderPrefs.account_id == account.id,
            OutlookFolderPrefs.folder_id == folder_id,
        )
        .one_or_none()
    )
    return _to_folder_out(folder, prefs)


@router.delete(
    "/folders/{folder_id}",
    response_model=GenericOk,
    dependencies=[Depends(require_module("email"))],
)
def delete_folder_route(
    folder_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> GenericOk:
    _require_admin(user)
    uid = _user_id(user)
    tid = _tenant_id(user)
    account = _account_for_user(tenant_db, uid)
    folder = (
        tenant_db.query(OutlookFolder)
        .filter(
            OutlookFolder.account_id == account.id,
            OutlookFolder.graph_folder_id == folder_id,
        )
        .one_or_none()
    )
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")
    if (folder.well_known_name or "") in SYSTEM_WELL_KNOWN:
        raise HTTPException(status_code=400, detail="cannot delete a system folder")
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            gc.delete_folder(folder_id)
    except OutlookReconnectRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OutlookGraphAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Graph error: {exc.status_code}") from exc

    # Cascade local: messages, prefs, sync_state, folder row.
    tenant_db.query(OutlookMessage).filter(
        OutlookMessage.account_id == account.id,
        OutlookMessage.folder_id == folder_id,
    ).delete(synchronize_session=False)
    tenant_db.query(OutlookFolderSyncState).filter(
        OutlookFolderSyncState.account_id == account.id,
        OutlookFolderSyncState.folder_id == folder_id,
    ).delete(synchronize_session=False)
    tenant_db.query(OutlookFolderPrefs).filter(
        OutlookFolderPrefs.account_id == account.id,
        OutlookFolderPrefs.folder_id == folder_id,
    ).delete(synchronize_session=False)
    tenant_db.delete(folder)
    tenant_db.commit()
    return GenericOk(ok=True, detail="deleted")


@router.post(
    "/folders/{folder_id}/empty",
    response_model=GenericOk,
    dependencies=[Depends(require_module("email"))],
)
def empty_folder_route(
    folder_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> GenericOk:
    """Delete every message in the folder. Iterates pages of message ids
    and DELETEs each via Graph. Local rows are deleted to mirror."""
    _require_admin(user)
    uid = _user_id(user)
    tid = _tenant_id(user)
    account = _account_for_user(tenant_db, uid)
    folder = (
        tenant_db.query(OutlookFolder)
        .filter(
            OutlookFolder.account_id == account.id,
            OutlookFolder.graph_folder_id == folder_id,
        )
        .one_or_none()
    )
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            gc.empty_folder(folder_id)
    except OutlookReconnectRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OutlookGraphAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Graph error: {exc.status_code}") from exc

    tenant_db.query(OutlookMessage).filter(
        OutlookMessage.account_id == account.id,
        OutlookMessage.folder_id == folder_id,
    ).delete(synchronize_session=False)
    tenant_db.commit()
    return GenericOk(ok=True, detail="emptied")


@router.post(
    "/folders/{folder_id}/mark-all-read",
    response_model=GenericOk,
    dependencies=[Depends(require_module("email"))],
)
def mark_all_read_route(
    folder_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> GenericOk:
    """Mark every message in the folder as read in Graph. Local mirror
    updated. No admin requirement — this is a per-user view-state change."""
    uid = _user_id(user)
    tid = _tenant_id(user)
    account = _account_for_user(tenant_db, uid)
    unread_rows = (
        tenant_db.query(OutlookMessage)
        .filter(
            OutlookMessage.account_id == account.id,
            OutlookMessage.folder_id == folder_id,
            OutlookMessage.is_read.is_(False),
        )
        .all()
    )
    if not unread_rows:
        return GenericOk(ok=True, detail="no unread messages")
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            for r in unread_rows:
                try:
                    gc.mark_message_read(r.graph_message_id, is_read=True)
                    r.is_read = True
                except OutlookGraphAPIError as exc:
                    log.warning("mark_all_read: failed for %s: %s", r.graph_message_id, exc)
    except OutlookReconnectRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    tenant_db.commit()
    return GenericOk(ok=True, detail=f"marked {len(unread_rows)} messages")


@router.patch(
    "/messages/{message_id}/read",
    response_model=GenericOk,
    dependencies=[Depends(require_module("email"))],
)
def patch_message_read_route(
    message_id: UUID,
    payload: MessageReadIn,
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> GenericOk:
    """Mark a single message read or unread in Microsoft + local mirror."""
    uid = _user_id(user)
    tid = _tenant_id(user)
    account = _account_for_user(tenant_db, uid)
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None or msg.account_id != account.id:
        raise HTTPException(status_code=404, detail="message not found")
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            gc.mark_message_read(msg.graph_message_id, is_read=payload.is_read)
    except OutlookReconnectRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OutlookGraphAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Graph error: {exc.status_code}") from exc
    msg.is_read = payload.is_read
    tenant_db.commit()
    return GenericOk(ok=True)


@router.post(
    "/messages/{message_id}/move",
    response_model=GenericOk,
    dependencies=[Depends(require_module("email"))],
)
def move_message_route(
    message_id: UUID,
    payload: MessageMoveIn,
    user: dict[str, Any] = Depends(get_current_user),
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> GenericOk:
    """Move a message to another folder. Graph creates a new message at
    the destination with a new id; the source is deleted. We mirror by
    updating folder_id locally — the next delta sync of source/dest folders
    will surface the actual remove/add events and reconcile graph_message_id
    via internet_message_id matching."""
    uid = _user_id(user)
    tid = _tenant_id(user)
    account = _account_for_user(tenant_db, uid)
    msg = tenant_db.get(OutlookMessage, message_id)
    if msg is None or msg.account_id != account.id:
        raise HTTPException(status_code=404, detail="message not found")
    dest_folder = (
        tenant_db.query(OutlookFolder)
        .filter(
            OutlookFolder.account_id == account.id,
            OutlookFolder.graph_folder_id == payload.destination_folder_id,
        )
        .one_or_none()
    )
    if dest_folder is None:
        raise HTTPException(status_code=404, detail="destination folder not found")
    try:
        with with_outlook_client(control_db, tenant_db, uid, tid) as gc:
            new = gc.move_message(msg.graph_message_id, dest_folder_id=payload.destination_folder_id)
    except OutlookReconnectRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OutlookGraphAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Graph error: {exc.status_code}") from exc
    # Update local mirror — graph_message_id changes on move.
    new_id = new.get("id")
    if new_id and new_id != msg.graph_message_id:
        msg.graph_message_id = new_id
    msg.folder_id = payload.destination_folder_id
    msg.folder_display_name = dest_folder.display_name
    tenant_db.commit()
    return GenericOk(ok=True, detail="moved")
