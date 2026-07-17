"""Resolve a human-readable name for the user who did something.

Every caller that wanted one used to guess it off the auth dict:

    user.get("name") or user.get("full_name") or user.get("email")

That guess never succeeds. The access token carries **sub, role, tenant_id,
exp, jti, typ** and nothing else — no name, no email — so both note writers
(routers/notes.py and routers/mobile.py) wrote NULL into
``job_notes.author_name`` for every note ever created. Production: 11 notes, 11
with an author_id, **0 with a name**, and JobDetailView renders
``note.author_name || 'Unknown'`` — so the office could not tell who wrote a
single note on any job. Silent, because NULL is a legal value for that column
and "Unknown" looks like a display default rather than a bug.

The id is the thing we actually have, so resolve from it and go to the DB. The
name is stored denormalized on the row on purpose: a note is a record of who
said what at a point in time, and it must not silently re-attribute itself if
the user is later renamed or deleted.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def _from_auth_dict(user: Any) -> str | None:
    """A name off the auth dict, if some auth path ever puts one there.

    Tried first because it costs nothing. Do not rely on it: the JWT path —
    which is every real request — has none of these keys. Test fixtures and
    service callers sometimes pass a fuller dict.
    """
    if not isinstance(user, dict):
        return None
    for key in ("name", "full_name", "username", "email"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _user_id_of(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    for key in ("user_id", "sub", "id"):
        value = user.get(key)
        if value:
            return str(value)
    return None


def resolve_author_name(db: Session, user: Any, *, user_id: str | None = None) -> str | None:
    """Best human-readable label for `user`, or None if there truly isn't one.

    Returns None rather than a placeholder: the caller's column is nullable and
    a fabricated "Unknown"/"System" would be indistinguishable from a real name
    downstream. Let the reader decide how to render an absent one.

    Never raises. A note must not fail to save because we couldn't pretty up a
    name — the body is the thing worth keeping.
    """
    direct = _from_auth_dict(user)
    if direct:
        return direct

    uid = user_id or _user_id_of(user)
    if not uid:
        return None

    # Import here: models import routers in places, and core is imported early.
    from gdx_dispatch.models.tenant_models import User

    try:
        # User.id is Uuid(as_uuid=True). Passing the raw string works on
        # Postgres and silently matches nothing on SQLite, where the column is
        # stored as 32-hex — so hand it a real UUID and let SQLAlchemy encode
        # it for whichever backend is under us.
        try:
            key: Any = UUID(str(uid))
        except (ValueError, AttributeError, TypeError):
            return None
        row = db.execute(select(User).where(User.id == key)).scalar_one_or_none()
    except Exception:  # pragma: no cover - defensive, see docstring
        log.exception("resolve_author_name_failed")
        return None

    if row is None:
        return None
    for value in (row.name, row.full_name, row.username, row.email):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
