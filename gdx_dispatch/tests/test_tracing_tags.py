"""SS-11 Slice A — tests for ``PlatformTracingMiddleware``.

The middleware is exercised by calling ``dispatch`` directly under an
explicit OpenTelemetry span so we can read back what attributes were
written. All tests are fail-open assertions: missing state must not
raise and must not stamp default/empty values onto the span.

A single TracerProvider is installed once per module (OTel only honours
the first ``set_tracer_provider`` call); each test gets its own
``InMemorySpanExporter`` attached as a SimpleSpanProcessor and detached
again at teardown so spans never leak between tests.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from gdx_dispatch.core.middleware.tracing import PlatformTracingMiddleware

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def tracer_provider() -> TracerProvider:
    """Install a real TracerProvider exactly once for this module."""
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        return existing
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    return provider


@pytest.fixture
def span_exporter(tracer_provider: TracerProvider) -> InMemorySpanExporter:
    """Attach a fresh in-memory exporter for the duration of a test."""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    tracer_provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()
        exporter.clear()


@pytest.fixture
def middleware() -> PlatformTracingMiddleware:
    async def _passthrough(_scope, _receive, _send):  # pragma: no cover
        return None

    return PlatformTracingMiddleware(app=_passthrough)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_request(state_overrides: dict[str, Any] | None = None) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/v1/probe",
        "raw_path": b"/v1/probe",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
        "state": {},
    }
    request = Request(scope)
    for key, value in (state_overrides or {}).items():
        setattr(request.state, key, value)
    return request


def _run_under_span(
    middleware: PlatformTracingMiddleware,
    request: Request,
    span_name: str = "test-request",
) -> Response:
    """Run ``middleware.dispatch`` inside an explicit recording span."""
    tracer = trace.get_tracer(__name__)

    async def call_next(_req: Request) -> Response:
        return PlainTextResponse("ok", status_code=200)

    async def _invoke() -> Response:
        with tracer.start_as_current_span(span_name):
            return await middleware.dispatch(request, call_next)

    return asyncio.run(_invoke())


def _only_finished_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected exactly one finished span, got {len(spans)}"
    return spans[0]


# ── attributes set when state is present ─────────────────────────────────────


def test_dispatch_tags_all_three_attributes_when_state_present(
    span_exporter, middleware
):
    principal = SimpleNamespace(installation_id="install-xyz")
    request = _make_request(
        {
            "tenant": {"id": "tenant-abc"},
            "principal": principal,
            "acting_on_tenant_id": "tenant-other",
        }
    )

    response = _run_under_span(middleware, request)

    assert response.status_code == 200
    span = _only_finished_span(span_exporter)
    assert span.attributes["gdx_dispatch.tenant_id"] == "tenant-abc"
    assert span.attributes["gdx_dispatch.installation_id"] == "install-xyz"
    assert span.attributes["gdx_dispatch.acting_on_tenant_id"] == "tenant-other"


def test_dispatch_handles_tenant_object_with_id_attribute(
    span_exporter, middleware
):
    request = _make_request({"tenant": SimpleNamespace(id="tenant-obj")})

    _run_under_span(middleware, request)

    span = _only_finished_span(span_exporter)
    assert span.attributes["gdx_dispatch.tenant_id"] == "tenant-obj"
    assert "gdx_dispatch.installation_id" not in span.attributes
    assert "gdx_dispatch.acting_on_tenant_id" not in span.attributes


def test_dispatch_stringifies_uuid_like_values(span_exporter, middleware):
    from uuid import UUID

    install_uuid = UUID("11111111-1111-1111-1111-111111111111")
    request = _make_request(
        {"principal": SimpleNamespace(installation_id=install_uuid)}
    )

    _run_under_span(middleware, request)

    span = _only_finished_span(span_exporter)
    assert span.attributes["gdx_dispatch.installation_id"] == str(install_uuid)


# ── fail-open behaviour ──────────────────────────────────────────────────────


def test_dispatch_no_state_does_not_raise_or_set_defaults(
    span_exporter, middleware
):
    request = _make_request()  # no overrides at all

    response = _run_under_span(middleware, request)

    assert response.status_code == 200
    span = _only_finished_span(span_exporter)
    assert "gdx_dispatch.tenant_id" not in span.attributes
    assert "gdx_dispatch.installation_id" not in span.attributes
    assert "gdx_dispatch.acting_on_tenant_id" not in span.attributes


def test_dispatch_partial_state_only_tags_present_fields(
    span_exporter, middleware
):
    request = _make_request({"tenant": {"id": "tenant-only"}})

    _run_under_span(middleware, request)

    span = _only_finished_span(span_exporter)
    assert span.attributes["gdx_dispatch.tenant_id"] == "tenant-only"
    assert "gdx_dispatch.installation_id" not in span.attributes
    assert "gdx_dispatch.acting_on_tenant_id" not in span.attributes


def test_dispatch_principal_without_installation_id_does_not_tag(
    span_exporter, middleware
):
    request = _make_request(
        {"principal": SimpleNamespace(installation_id=None)}
    )

    _run_under_span(middleware, request)

    span = _only_finished_span(span_exporter)
    assert "gdx_dispatch.installation_id" not in span.attributes


def test_dispatch_empty_string_values_do_not_tag(span_exporter, middleware):
    request = _make_request(
        {
            "tenant": {"id": ""},
            "principal": SimpleNamespace(installation_id=""),
            "acting_on_tenant_id": "",
        }
    )

    _run_under_span(middleware, request)

    span = _only_finished_span(span_exporter)
    assert "gdx_dispatch.tenant_id" not in span.attributes
    assert "gdx_dispatch.installation_id" not in span.attributes
    assert "gdx_dispatch.acting_on_tenant_id" not in span.attributes


def test_dispatch_outside_active_span_passes_through_silently(
    span_exporter, middleware
):
    """With no active span, dispatch must still deliver the response and
    record nothing — ``get_current_span()`` returns the invalid sentinel,
    which reports ``is_recording() == False``.
    """
    request = _make_request({"tenant": {"id": "tenant-xyz"}})

    async def call_next(_req: Request) -> Response:
        return PlainTextResponse("ok", status_code=200)

    response = asyncio.run(middleware.dispatch(request, call_next))
    assert response.status_code == 200
    assert span_exporter.get_finished_spans() == ()


def test_dispatch_swallow_extraction_exceptions(span_exporter, middleware):
    """A descriptor that raises must not abort the response."""

    class _Boom:
        @property
        def installation_id(self) -> str:
            raise RuntimeError("synthetic-extraction-failure")

    request = _make_request(
        {"tenant": {"id": "tenant-abc"}, "principal": _Boom()}
    )

    response = _run_under_span(middleware, request)
    assert response.status_code == 200
    span = _only_finished_span(span_exporter)
    # tenant landed first; principal extraction blew up before installation
    # attr was written, so the failure is swallowed and only the safe tag
    # remains on the span.
    assert span.attributes["gdx_dispatch.tenant_id"] == "tenant-abc"
    assert "gdx_dispatch.installation_id" not in span.attributes
