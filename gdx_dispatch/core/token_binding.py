"""SS-10 Slice E — token-binding dependency stub.

Placeholder factory for RFC 8705 / OAuth 2.0 mutual-TLS or DPoP-style
``cnf``-bound access tokens. This slice ships the signature only; the
returned dependency is a no-op so future wiring in routers can adopt the
shape today without introducing a functional gate yet. Enforcement lands
in a later SS-10 slice once the ``Principal`` shape carries the bound
confirmation claim.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def require_token_binding(bind_to: str = "ip") -> Callable[..., None]:
    """Return a no-op FastAPI dependency reserving the binding signature.

    ``bind_to`` documents the intended binding source for this endpoint
    (``"ip"``, ``"cnf"``, ``"dpop"``, etc.) so callers can declare intent
    now. The returned callable accepts any args/kwargs a dependency
    injector might hand it and always returns ``None``.
    """

    def _dependency(*_args: Any, **_kwargs: Any) -> None:
        return None

    _dependency.__name__ = f"require_token_binding_{bind_to}"
    _dependency.__doc__ = (
        f"No-op token-binding stub (bind_to={bind_to!r}). "
        "Replaced by cnf-claim enforcement in a later SS-10 slice."
    )
    return _dependency
