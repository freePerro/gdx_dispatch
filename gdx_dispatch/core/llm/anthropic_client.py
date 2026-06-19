"""Per-tenant Anthropic client factory.

Sprint 1.x-S5. Reads the tenant's stored API key (S4 ``key_storage.get_key``)
and returns a fresh ``anthropic.Anthropic`` instance. No caching — the caller
is responsible for re-using the client within a request scope.
"""
from __future__ import annotations

from uuid import UUID

from anthropic import Anthropic
from sqlalchemy.orm import Session

from gdx_dispatch.core.llm.key_storage import get_key


class LLMNotConfigured(RuntimeError):
    """Raised when the tenant has no LLM provider key set."""


def get_client(db: Session, tenant_id: UUID) -> Anthropic:
    key = get_key(db, tenant_id)
    if key is None:
        raise LLMNotConfigured(f"tenant {tenant_id} has no LLM provider key set")
    return Anthropic(api_key=key)
