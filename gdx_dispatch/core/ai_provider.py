"""Pluggable AI provider — templates by default, local AI when configured.

Tenants can set AI_PROVIDER_URL in their settings to point to any
OpenAI-compatible endpoint (llama-swap, vLLM, or any /v1/chat/completions
server). If not set, falls back to built-in templates.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

# Default local AI endpoint — can be overridden per tenant via settings
DEFAULT_AI_URL = os.getenv("AI_PROVIDER_URL", "")
DEFAULT_AI_MODEL = os.getenv("AI_PROVIDER_MODEL", "gemma-4-26b-a4b")
DEFAULT_AI_KEY = os.getenv("AI_PROVIDER_KEY", "localai")


async def generate(
    prompt: str,
    system: str = "You are a helpful assistant for a garage door service company.",
    ai_url: str | None = None,
    ai_model: str | None = None,
    ai_key: str | None = None,
    max_tokens: int = 500,
) -> str | None:
    """Call an OpenAI-compatible AI endpoint. Returns None if not configured or fails."""
    url = ai_url or DEFAULT_AI_URL
    if not url:
        return None

    model = ai_model or DEFAULT_AI_MODEL
    key = ai_key or DEFAULT_AI_KEY

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"].get("content", "")
            return content if content else None
    except Exception:  # Returns None on failure as per contract.
        log.exception("ai_provider_call_failed url=%s", url)
        return None


def generate_sync(
    prompt: str,
    system: str = "You are a helpful assistant for a garage door service company.",
    ai_url: str | None = None,
    ai_model: str | None = None,
    ai_key: str | None = None,
    max_tokens: int = 500,
) -> str | None:
    """Synchronous version for use in non-async routes."""
    url = ai_url or DEFAULT_AI_URL
    if not url:
        return None

    model = ai_model or DEFAULT_AI_MODEL
    key = ai_key or DEFAULT_AI_KEY

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"].get("content", "")
            return content if content else None
    except Exception:
        log.exception("ai_provider_sync_failed url=%s", url)
        return None
