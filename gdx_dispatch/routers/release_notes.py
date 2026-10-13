"""Release notes for every signed-in user — the What's new page and its history.

GET /api/release-notes            — every bundled release, newest first
GET /api/release-notes/{version}  — one release (``1.2.3`` or ``v1.2.3``)

Read-only and served from files inside the image (services/release_notes.py),
so no audit row. Readable by any signed-in user: techs and office staff are
the people asking "why does this work differently now".
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from gdx_dispatch.core.app_version import installed_version, parse_semver
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.services.release_notes import bundled_releases

router = APIRouter(prefix="/api/release-notes", tags=["release-notes"])


@router.get("")
def list_release_notes(_user: Any = Depends(get_current_user)) -> dict[str, Any]:
    """``installed_version`` is ``None`` on a dev build."""
    return {
        "installed_version": installed_version(),
        "releases": [r.to_dict() for r in bundled_releases()],
    }


@router.get("/{version}")
def get_release_note(version: str, _user: Any = Depends(get_current_user)) -> dict[str, Any]:
    parts = parse_semver(version)
    if parts is None:
        raise HTTPException(status_code=404, detail="no such release")
    wanted = ".".join(map(str, parts))
    for r in bundled_releases():
        if r.version == wanted:
            return r.to_dict()
    raise HTTPException(status_code=404, detail="no such release")
