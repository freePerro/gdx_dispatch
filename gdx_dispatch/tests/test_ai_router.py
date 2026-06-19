from __future__ import annotations

import asyncio
import base64
import os
import sys
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.delenv("AI_CLOUD_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("AI_LOCAL_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_CLOUD_MODEL", raising=False)
    monkeypatch.delenv("AI_CLOUD_MODEL_FAST", raising=False)
    monkeypatch.delenv("AI_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("AI_RATE_LIMIT_PER_MIN", raising=False)

    from gdx_dispatch.core.ai_router import reset_ai_singletons

    reset_ai_singletons()


@pytest.fixture
def ai_module():
    import gdx_dispatch.core.ai_router as ai_router_module

    return ai_router_module


def _make_httpx_client(monkeypatch, ai_module, handler):
    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: float):
            return handler(url=url, headers=headers, json=json, timeout=timeout)

    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeAsyncClient)


# 1

def test_cloud_routing(monkeypatch, ai_module):
    monkeypatch.setenv("AI_PROVIDER", "cloud")
    router = ai_module.AIRouter()

    for task in ai_module.AITask:
        endpoint, _, _ = router._get_endpoint(task)
        assert endpoint == router.cloud_endpoint


# 2

def test_local_routing(monkeypatch, ai_module):
    monkeypatch.setenv("AI_PROVIDER", "local")
    router = ai_module.AIRouter()

    for task in ai_module.AITask:
        endpoint, api_key, model = router._get_endpoint(task)
        assert endpoint == router.local_endpoint
        assert api_key == ""
        assert model == router.local_model


# 3

def test_hybrid_routing(monkeypatch, ai_module):
    monkeypatch.setenv("AI_PROVIDER", "hybrid")
    router = ai_module.AIRouter()

    local_tasks = {
        ai_module.AITask.PHOTO_ANALYSIS,
        ai_module.AITask.DISPATCH_SUGGESTION,
        ai_module.AITask.SENTIMENT_ANALYSIS,
    }
    for task in local_tasks:
        endpoint, _, _ = router._get_endpoint(task)
        assert endpoint == router.local_endpoint

    endpoint, _, _ = router._get_endpoint(ai_module.AITask.ESTIMATE_GENERATION)
    assert endpoint == router.cloud_endpoint


# 4

def test_tenant_isolation(monkeypatch, ai_module):
    captured: dict[str, Any] = {}

    def _handler(**kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
            request=httpx.Request("POST", kwargs["url"]),
        )

    _make_httpx_client(monkeypatch, ai_module, _handler)

    router = ai_module.AIRouter()
    result = asyncio.run(router.generate(ai_module.AITask.GENERAL, "hello", "tenant-A"))

    assert result["content"] == "ok"
    assert captured["json"]["metadata"]["tenant_id"] == "tenant-A"


# 5

def test_logging(monkeypatch, ai_module):
    def _handler(**kwargs):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "done"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
            request=httpx.Request("POST", kwargs["url"]),
        )

    _make_httpx_client(monkeypatch, ai_module, _handler)

    router = ai_module.AIRouter()
    router.logger.log = AsyncMock()

    asyncio.run(router.generate(ai_module.AITask.GENERAL, "hello there", "tenant-1"))

    router.logger.log.assert_awaited_once()
    call_kwargs = router.logger.log.await_args.kwargs
    assert call_kwargs["tenant_id"] == "tenant-1"
    assert call_kwargs["input_tokens"] == 5
    assert call_kwargs["output_tokens"] == 7
    assert call_kwargs["cost"] > 0


# 6

def test_api_key_header(monkeypatch, ai_module):
    monkeypatch.setenv("AI_PROVIDER", "cloud")
    monkeypatch.setenv("AI_CLOUD_API_KEY", "secret-key")
    captured: dict[str, Any] = {}

    def _handler(**kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            request=httpx.Request("POST", kwargs["url"]),
        )

    _make_httpx_client(monkeypatch, ai_module, _handler)

    router = ai_module.AIRouter()
    asyncio.run(router.generate(ai_module.AITask.GENERAL, "ping", "tenant-1"))

    assert captured["headers"]["Authorization"] == "Bearer secret-key"


# 7

def test_fallback(monkeypatch, ai_module):
    monkeypatch.setenv("AI_PROVIDER", "hybrid")
    monkeypatch.setenv("AI_CLOUD_API_KEY", "cloud-key")
    calls: list[str] = []

    def _handler(**kwargs):
        calls.append(kwargs["url"])
        if kwargs["url"].startswith("http://localhost"):
            raise httpx.ConnectError("local down", request=httpx.Request("POST", kwargs["url"]))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "cloud fallback"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            request=httpx.Request("POST", kwargs["url"]),
        )

    _make_httpx_client(monkeypatch, ai_module, _handler)

    router = ai_module.AIRouter()
    result = asyncio.run(router.generate(ai_module.AITask.PHOTO_ANALYSIS, "inspect", "tenant-1"))

    assert result["content"] == "cloud fallback"
    assert len(calls) == 2
    assert calls[0].startswith("http://localhost")
    assert calls[1].startswith("https://")


# 8

def test_usage_endpoint(ai_module):
    logger = ai_module.get_ai_logger()
    asyncio.run(
        logger.log(
            tenant_id="tenant-usage",
            task="general",
            model="m",
            input_tokens=10,
            output_tokens=5,
            cost=0.12,
            latency_ms=12,
        )
    )
    asyncio.run(
        logger.log(
            tenant_id="tenant-usage",
            task="general",
            model="m",
            input_tokens=4,
            output_tokens=6,
            cost=0.08,
            latency_ms=10,
        )
    )

    from starlette.requests import Request

    body = asyncio.run(
        ai_module.ai_usage(
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/ai/usage",
                    "headers": [],
                    "state": {"tenant": {"id": "tenant-usage"}},
                }
            )
        )
    )

    assert body["tenant_id"] == "tenant-usage"
    assert body["requests"] == 2
    assert body["input_tokens"] == 14
    assert body["output_tokens"] == 11
    assert body["total_tokens"] == 25
    assert body["cost"] == pytest.approx(0.20)


# 9

def test_generate_with_tools(monkeypatch, ai_module):
    captured: dict[str, Any] = {}

    def _handler(**kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "tool result"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
            request=httpx.Request("POST", kwargs["url"]),
        )

    _make_httpx_client(monkeypatch, ai_module, _handler)

    router = ai_module.AIRouter()
    tools = [{"type": "function", "function": {"name": "lookup_job", "parameters": {"type": "object"}}}]
    result = asyncio.run(router.generate_with_tools(ai_module.AITask.GENERAL, "do it", "tenant-1", tools=tools))

    assert result["content"] == "tool result"
    assert captured["json"]["tools"] == tools


# 10

def test_rate_limiting(monkeypatch):
    import gdx_dispatch.core.ai_router as ai_router_module

    monkeypatch.setenv("AI_RATE_LIMIT_PER_MIN", "1")
    ai_router_module.reset_ai_singletons()

    limiter = ai_router_module.get_ai_rate_limiter()
    assert limiter.allow("tenant-limit") is True
    assert limiter.allow("tenant-limit") is False
    assert limiter.allow("tenant-other") is True


# 11

def test_cost_calculation(ai_module):
    router = ai_module.AIRouter()
    cost = router.calculate_cost("claude-sonnet-4-20250514", input_tokens=1000, output_tokens=1000)
    assert cost == pytest.approx(0.018, rel=1e-6)


# 12

def test_photo_analysis_endpoint(monkeypatch):
    class _StubRouter:
        async def generate(self, **kwargs):
            return {
                "content": "{" + '"damage_level":"minor","recommendation":"monitor"' + "}",
                "model": "stub",
                "tokens_used": 12,
            }

    import gdx_dispatch.core.ai_router as ai_router_module

    monkeypatch.setattr(ai_router_module, "get_ai_router", lambda: _StubRouter())

    from starlette.requests import Request

    image_data = base64.b64encode(b"fake-image").decode("ascii")
    from unittest.mock import MagicMock

    mock_db = MagicMock()

    body = asyncio.run(
        ai_router_module.analyze_photo(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/ai/analyze-photo",
                    "headers": [],
                    "state": {"tenant": {"id": "tenant-photo"}},
                }
            ),
            ai_router_module.AnalyzePhotoRequest(
                image_base64=image_data,
                prompt="Analyze this door",
            ),
            user={"user_id": "test-user", "role": "admin", "tenant_id": "tenant-photo"},
            db=mock_db,
        )
    )

    assert "analysis" in body
    assert "damage_level" in body["analysis"]


# 13 (extra safety)

def test_registered_in_main_app():
    with open("gdx_dispatch/app.py", encoding="utf-8") as f:
        app_source = f.read()

    assert "from gdx_dispatch.core.ai_router import router as ai_router_router" in app_source
    assert "app.include_router(ai_router_router)" in app_source
