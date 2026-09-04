"""Auth-core composition surface.

Re-exports :func:`gdx_dispatch.routers.auth.get_current_user` so router-level
dependency injection keeps resolving to the real JWT decoder.

This module used to also expose ``validate_principal``, a wrapper over the
Authentik access-token validator in ``gdx_dispatch.core.auth_jwt``. Authentik is
gone — no container, no ``AUTHENTIK_*`` variables set, and its issuer was
hard-coded to a placeholder host — and the validator could never have accepted
a token this app mints: ``_issue()`` emits no ``iss`` claim, so every real
token was rejected with ``MalformedToken`` and fell through to the local
decode. The wrapper, the validator and the ``Principal`` type it returned have
all been removed.
"""
from __future__ import annotations

from gdx_dispatch.routers.auth import get_current_user  # noqa: F401  (legacy re-export)

__all__ = ["get_current_user"]
