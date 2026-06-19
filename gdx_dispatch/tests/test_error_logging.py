from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from gdx_dispatch.app import configure_json_logging, create_app
from gdx_dispatch.core.error_handler import global_exception_handler
from gdx_dispatch.core.request_logging import RequestLoggingMiddleware


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.add_exception_handler(Exception, global_exception_handler)
    app.add_exception_handler(IntegrityError, global_exception_handler)
    app.add_exception_handler(ValueError, global_exception_handler)
    app.add_exception_handler(HTTPException, global_exception_handler)

    @app.get("/ok")
    async def ok(request: Request):
        request.state.tenant = {"id": "tenant-123"}
        return {"ok": True}

    @app.get("/err")
    async def err():
        raise RuntimeError("boom")

    @app.get("/val")
    async def val():
        raise ValueError("bad value")

    @app.get("/integrity")
    async def integrity():
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    return app


async def _get(app: FastAPI, path: str):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


@pytest.mark.anyio
async def test_request_id_in_response_header():
    response = await _get(_build_test_app(), "/ok")
    assert response.status_code == 200
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) == 8


@pytest.mark.anyio
async def test_request_logged_with_duration(caplog):
    caplog.set_level(logging.INFO, logger="gdx_dispatch.requests")
    response = await _get(_build_test_app(), "/ok")
    assert response.status_code == 200
    records = [r for r in caplog.records if r.name == "gdx_dispatch.requests" and r.msg == "request_complete"]
    assert records
    assert isinstance(records[-1].duration_ms, int)
    assert records[-1].duration_ms >= 0


@pytest.mark.anyio
async def test_500_error_logged_with_traceback(caplog):
    caplog.set_level(logging.ERROR, logger="gdx_dispatch.error_handler")
    response = await _get(_build_test_app(), "/err")
    assert response.status_code == 500
    records = [r for r in caplog.records if r.name == "gdx_dispatch.error_handler" and "global_exception" in r.getMessage()]
    assert records
    assert "RuntimeError" in records[-1].getMessage()


@pytest.mark.anyio
async def test_integrity_error_returns_409(caplog):
    caplog.set_level(logging.WARNING, logger="gdx_dispatch")
    response = await _get(_build_test_app(), "/integrity")
    assert response.status_code == 409
    assert response.json()["detail"] == "Data conflict"
    records = [r for r in caplog.records if r.name == "gdx_dispatch.error_handler" and "IntegrityError" in r.getMessage()]
    assert records


@pytest.mark.anyio
async def test_runtime_error_returns_500_with_generic_detail():
    """RuntimeError should return 500 with a generic message (no internal detail leaked)."""
    response = await _get(_build_test_app(), "/err")
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Internal server error"
    assert body["error_type"] == "RuntimeError"


@pytest.mark.anyio
async def test_tenant_id_in_log_context(caplog):
    caplog.set_level(logging.INFO, logger="gdx_dispatch.requests")
    response = await _get(_build_test_app(), "/ok")
    assert response.status_code == 200
    records = [r for r in caplog.records if r.name == "gdx_dispatch.requests" and r.msg == "request_complete"]
    assert records
    assert records[-1].tenant_id == "tenant-123"


def test_json_log_format():
    stream = io.StringIO()
    configure_json_logging(level="INFO", stream=stream)
    logging.getLogger("gdx_dispatch.json.test").info(
        "json_event",
        extra={"request_id": "req-1", "tenant_id": "tenant-1"},
    )
    line = stream.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "json_event"
    assert payload["request_id"] == "req-1"
    assert payload["tenant_id"] == "tenant-1"


@pytest.mark.anyio
async def test_unhandled_error_doesnt_leak_traceback_to_client():
    response = await _get(_build_test_app(), "/err")
    assert response.status_code == 500
    detail = response.json()["detail"].lower()
    assert "traceback" not in detail
    assert "runtimeerror" not in detail


def test_app_registers_request_logging_middleware():
    app = create_app()
    assert any(m.cls is RequestLoggingMiddleware for m in app.user_middleware)


def test_app_registers_global_exception_handlers():
    app = create_app()
    assert Exception in app.exception_handlers
    assert IntegrityError in app.exception_handlers
    assert ValueError in app.exception_handlers
    assert HTTPException in app.exception_handlers


@pytest.mark.anyio
async def test_value_error_returns_400_with_message():
    response = await _get(_build_test_app(), "/val")
    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "bad value"
    assert body["error_type"] == "ValueError"
