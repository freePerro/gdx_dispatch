"""SS-11 DR3-env — diagnostic sibling of ``test_policy_tracing.py``.

Purpose: capture the OTel/Python environment slice and a thread-stack
traceback if the SS-11D real-ASGI proof test (``test_middleware_and_
policy_compose_in_real_asgi_request``) hangs under the timeout-scoped
replay observed on commit ``0161f80``.

This file is intentionally a *sibling* of ``test_policy_tracing.py``:

* It does NOT import or modify that module.
* It re-implements only the one ASGI composition test under
  diagnostics, with the same middleware + policy-probe semantics.
* It arms ``signal.SIGALRM`` at t+60s before the test body runs so a
  hang gets a thread dump captured *before* the wrapping
  ``timeout 90`` reaper kills the process.
* It emits per-variable presence checks (NOT ``env``/``printenv``) for
  the OTel/Python env vars Supervisor flagged as candidate variance
  surface.

Diagnostic only — no fix is attempted in this slice.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import sys

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from gdx_dispatch.core.policy import (
    ALLOW_SAME_TENANT_ALLOWED_ACTION,
    ResourceRef,
    evaluate,
)
from gdx_dispatch.core.principal import ActorKind, Principal

TENANT_ALPHA = "tenant-alpha-uuid"
TENANT_BRAVO = "tenant-bravo-uuid"

# OTel + Python env vars Supervisor named as candidate variance surface
# for the SS-11D hang. Presence-only check — the *value* is never read
# or printed, so this stays credential-safe even if a future operator
# stuffs a token into one of these by accident.
_ENV_VARS = (
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_METRICS_EXPORTER",
    "PYTHONPATH",
    "PYTEST_ADDOPTS",
    "PYTHONUNBUFFERED",
)


def _print_env_slice() -> None:
    """Per-variable presence printout. Never prints the value."""
    print("[ss11-dr3-env] env-var presence slice:")
    for name in _ENV_VARS:
        present = name in os.environ
        print(f"[ss11-dr3-env] {name}={'set' if present else 'unset'}")
    sys.stdout.flush()


def _arm_sigalrm_traceback(seconds: int = 60) -> None:
    """Arm SIGALRM at t+``seconds`` to dump all thread tracebacks.

    The wrapping ``timeout 90`` reaper sends SIGTERM at t+90s; arming
    at t+60s leaves a 30-second window for ``faulthandler`` to emit
    stacks for every thread before the process is killed. On platforms
    without SIGALRM (Windows) this is a silent no-op so the test still
    imports.
    """
    if not hasattr(signal, "SIGALRM"):
        print("[ss11-dr3-env] SIGALRM unavailable on this platform; skipping arm")
        return

    faulthandler.enable()

    def _on_alarm(signum, frame):  # pragma: no cover - only fires on hang
        sys.stderr.write(
            "[ss11-dr3-env] SIGALRM fired at t+60s — dumping all thread stacks\n"
        )
        sys.stderr.flush()
        faulthandler.dump_traceback(all_threads=True)
        sys.stderr.flush()

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(seconds)
    print(f"[ss11-dr3-env] armed SIGALRM at t+{seconds}s for traceback dump")
    sys.stdout.flush()


def _disarm_sigalrm() -> None:
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)


# ── fixtures (sibling of test_policy_tracing.py — independent provider) ──────


@pytest.fixture(scope="module")
def tracer_provider() -> TracerProvider:
    """Reuse the global TracerProvider if one is already installed
    (OTel honours only the first ``set_tracer_provider`` call)."""
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        return existing
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    return provider


@pytest.fixture
def span_exporter(tracer_provider: TracerProvider) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    tracer_provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()
        exporter.clear()


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_principal(*, tenant_id: str = TENANT_ALPHA) -> Principal:
    return Principal(
        tenant_id=tenant_id,
        subject="authentik-user-123",
        provider="gdx-spa",
        actor_kind=ActorKind.HUMAN,
        identity_type="human",
        issued_at=1_700_000_000,
        expires_at=1_700_003_600,
        issuer="https://auth.example.com/application/o/gdx-spa/",
        audience="gdx-api",
    )


# ── the diagnostic test ─────────────────────────────────────────────────────


def test_env_capture_real_asgi_request(span_exporter):
    """Diagnostic sibling of
    ``test_middleware_and_policy_compose_in_real_asgi_request``.

    Steps:

    1. Print the env-var presence slice (no values).
    2. Arm SIGALRM at t+60s so a hang triggers a thread-stack dump
       before ``timeout 90`` reaps the process.
    3. Run the same minimal-FastAPI + ``PlatformTracingMiddleware``
       composition the SS-11D test uses.
    4. Assert lineage exactly as SS-11D would, so a green run here
       proves the env slice is benign for this hash; a red/timeout
       run gives Supervisor the thread-dump payload.
    """
    # Step 1+2: env probe + alarm arm BEFORE any ASGI work.
    _print_env_slice()
    _arm_sigalrm_traceback(seconds=60)

    try:
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from gdx_dispatch.core.middleware.tracing import PlatformTracingMiddleware

        class _OuterRequestSpanMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return
                state = scope.setdefault("state", {})
                state["tenant"] = {"id": TENANT_ALPHA}
                state["acting_on_tenant_id"] = TENANT_BRAVO
                tracer = trace.get_tracer(__name__)
                with tracer.start_as_current_span("gdx_dispatch.request"):
                    await self.app(scope, receive, send)

        app = FastAPI()

        @app.get("/policy-probe")
        def probe():
            principal = _make_principal(tenant_id=TENANT_ALPHA)
            resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")
            decision = evaluate(principal, "read", resource)
            return {"allowed": decision.allowed, "reason": decision.reason}

        app.add_middleware(PlatformTracingMiddleware)
        app.add_middleware(_OuterRequestSpanMiddleware)

        with TestClient(app) as client:
            response = client.get("/policy-probe")

        assert response.status_code == 200
        body = response.json()
        assert body["allowed"] is True
        assert body["reason"] == ALLOW_SAME_TENANT_ALLOWED_ACTION

        finished = span_exporter.get_finished_spans()
        request_spans = [s for s in finished if s.name == "gdx_dispatch.request"]
        policy_spans = [s for s in finished if s.name == "policy.decision"]
        assert len(request_spans) == 1, [s.name for s in finished]
        assert len(policy_spans) == 1, [s.name for s in finished]
        request_span = request_spans[0]
        policy_span = policy_spans[0]

        assert policy_span.context.trace_id == request_span.context.trace_id
        assert policy_span.parent is not None
        assert policy_span.parent.span_id == request_span.context.span_id
        assert request_span.parent is None

        assert request_span.attributes["gdx_dispatch.tenant_id"] == TENANT_ALPHA
        assert request_span.attributes["gdx_dispatch.acting_on_tenant_id"] == TENANT_BRAVO
        assert "gdx_dispatch.tenant_id" not in policy_span.attributes
        assert "gdx_dispatch.acting_on_tenant_id" not in policy_span.attributes

        assert policy_span.attributes["policy.decision.reason"] == (
            ALLOW_SAME_TENANT_ALLOWED_ACTION
        )
        assert policy_span.attributes["policy.decision.capability_matched"] is True
        assert "policy.decision.reason" not in request_span.attributes
        assert "policy.decision.capability_matched" not in request_span.attributes
    finally:
        _disarm_sigalrm()
