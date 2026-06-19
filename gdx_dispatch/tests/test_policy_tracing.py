"""SS-11 Slice B — tests for ``gdx_dispatch.core.policy.evaluate`` child-span tracing.

Covers:

* positive path — one ``policy.decision`` span emitted with reason +
  capability_matched attributes for both allow and deny outcomes
* parentage — ``policy.decision`` is a CHILD of the active outer span
  (i.e. SS-11A's request/tenant span or any caller-supplied span)
* principal-role emission — attribute lands when the principal exposes
  a non-empty ``role`` and is absent otherwise
* fail-open — non-recording tracer (no provider installed) still
  returns the correct :class:`Decision`
* fail-open — a raising ``role`` descriptor swallows the error and
  leaves the span without the optional attribute

Uses the same fixture shape as ``test_tracing_tags.py``: one
module-scoped ``TracerProvider`` (OTel refuses double-set) plus a
per-test ``InMemorySpanExporter`` attached via ``SimpleSpanProcessor``.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from gdx_dispatch.core.policy import (
    ALLOW_SAME_TENANT_ALLOWED_ACTION,
    DENY_CROSS_TENANT,
    ResourceRef,
    evaluate,
)
from gdx_dispatch.core.principal import ActorKind, Principal

TENANT_ALPHA = "tenant-alpha-uuid"
TENANT_BRAVO = "tenant-bravo-uuid"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def tracer_provider() -> TracerProvider:
    """Install a real TracerProvider exactly once for this module.

    OTel's ``set_tracer_provider`` only honours the first call, so if a
    sibling test module (e.g. ``test_tracing_tags.py``) already
    installed one we reuse it. The module-level ``_TRACER`` that
    ``gdx_dispatch.core.policy`` resolved at import time is bound to whatever
    provider is current when it first calls
    ``get_tracer(__name__).start_as_current_span(...)``, so provider
    identity is consistent either way.
    """
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        return existing
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    return provider


@pytest.fixture
def span_exporter(tracer_provider: TracerProvider) -> InMemorySpanExporter:
    """Fresh in-memory exporter per test; detach + clear at teardown."""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    tracer_provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()
        exporter.clear()


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_principal(
    *,
    tenant_id: str = TENANT_ALPHA,
    subject: str = "authentik-user-123",
    provider: str = "gdx-spa",
    actor_kind: ActorKind = ActorKind.HUMAN,
    identity_type: str = "human",
) -> Principal:
    return Principal(
        tenant_id=tenant_id,
        subject=subject,
        provider=provider,
        actor_kind=actor_kind,
        identity_type=identity_type,
        issued_at=1_700_000_000,
        expires_at=1_700_003_600,
        issuer="https://auth.example.com/application/o/gdx-spa/",
        audience="gdx-api",
    )


def _span_by_name(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, (
        f"expected exactly one span named {name!r}; "
        f"got {[s.name for s in exporter.get_finished_spans()]}"
    )
    return matches[0]


# ── positive path: allow decision ────────────────────────────────────────────


def test_allow_emits_policy_decision_span_with_expected_attrs(
    span_exporter,
):
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is True
    assert decision.reason == ALLOW_SAME_TENANT_ALLOWED_ACTION

    span = _span_by_name(span_exporter, "policy.decision")
    assert span.attributes["policy.decision.reason"] == ALLOW_SAME_TENANT_ALLOWED_ACTION
    assert span.attributes["policy.decision.capability_matched"] is True
    # role is absent on the vanilla Principal — attribute must not land
    assert "policy.decision.principal_role" not in span.attributes


# ── positive path: deny decision ─────────────────────────────────────────────


def test_deny_emits_policy_decision_span_with_false_capability_matched(
    span_exporter,
):
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_BRAVO, resource_type="job")

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_CROSS_TENANT

    span = _span_by_name(span_exporter, "policy.decision")
    assert span.attributes["policy.decision.reason"] == DENY_CROSS_TENANT
    assert span.attributes["policy.decision.capability_matched"] is False


# ── parentage ────────────────────────────────────────────────────────────────


def test_policy_decision_span_is_child_of_active_outer_span(
    span_exporter,
):
    """The policy span must parent to the caller's active span so audit
    pipelines can walk from a request span down to its policy decisions
    without losing the trace context."""
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("outer-request") as outer:
        outer_context = outer.get_span_context()
        evaluate(principal, "write", resource)

    policy_span = _span_by_name(span_exporter, "policy.decision")
    outer_span = _span_by_name(span_exporter, "outer-request")

    # trace id shared with outer span
    assert policy_span.context.trace_id == outer_context.trace_id
    # parent points at the outer span specifically
    assert policy_span.parent is not None
    assert policy_span.parent.span_id == outer_context.span_id
    # sibling relationships — outer is a root, policy is its child
    assert outer_span.parent is None


# ── optional principal.role attribute ────────────────────────────────────────


def test_principal_with_role_emits_principal_role_attribute(span_exporter):
    """Principal exposing a non-empty ``role`` lands the optional
    attribute. Kept defensive via ``getattr`` because the current
    :class:`Principal` dataclass does not declare a ``role`` field — a
    caller-supplied subclass, SS-9+ extension, or test-only shim may."""
    base = _make_principal(tenant_id=TENANT_ALPHA)
    principal_with_role = SimpleNamespace(
        **{f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()},
        role="admin",
    )
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal_with_role, "read", resource)

    assert decision.allowed is True
    span = _span_by_name(span_exporter, "policy.decision")
    assert span.attributes["policy.decision.principal_role"] == "admin"


def test_principal_with_empty_role_string_does_not_emit_role_attribute(
    span_exporter,
):
    base = _make_principal(tenant_id=TENANT_ALPHA)
    principal_empty_role = SimpleNamespace(
        **{f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()},
        role="",
    )
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    evaluate(principal_empty_role, "read", resource)

    span = _span_by_name(span_exporter, "policy.decision")
    assert "policy.decision.principal_role" not in span.attributes


# ── fail-open: tracing exceptions must not break correctness ─────────────────


def test_raising_role_descriptor_does_not_break_decision(span_exporter):
    """If the principal exposes a ``role`` descriptor that raises, the
    tag helper swallows the error and the decision is still returned
    intact. Validates the ``except Exception`` fail-open in
    ``_tag_decision_span``."""
    base = _make_principal(tenant_id=TENANT_ALPHA)
    base_fields = {
        f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()
    }

    class _BoomPrincipal:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        @property
        def role(self):
            raise RuntimeError("synthetic-role-descriptor-failure")

    principal = _BoomPrincipal(**base_fields)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal, "read", resource)

    # decision is unchanged — tracing failure did NOT mutate the result
    assert decision.allowed is True
    assert decision.reason == ALLOW_SAME_TENANT_ALLOWED_ACTION

    # span still landed with the mandatory attributes; role is absent
    span = _span_by_name(span_exporter, "policy.decision")
    assert span.attributes["policy.decision.reason"] == ALLOW_SAME_TENANT_ALLOWED_ACTION
    assert span.attributes["policy.decision.capability_matched"] is True
    assert "policy.decision.principal_role" not in span.attributes


# ── fail-open: decision semantics under no exporter ──────────────────────────


def test_evaluate_returns_correct_decision_without_exporter():
    """Without an attached ``SimpleSpanProcessor``, no span ever
    finishes — the policy decision must still return the exact same
    :class:`Decision` a caller would get pre-SS-11B. Guards the
    'tracing machinery unavailable' contract from the task brief."""
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    # NOTE: no span_exporter fixture — the provider has no processors
    # for this test, so span starts succeed but nothing is exported.
    decision = evaluate(principal, "read", resource)

    assert decision.allowed is True
    assert decision.reason == ALLOW_SAME_TENANT_ALLOWED_ACTION


# ── integration: SS-11A request-span + SS-11B policy-span composition ────────


def test_policy_decision_is_child_of_active_request_span(span_exporter):
    """SS-11C integration: when a caller (middleware chain, router,
    dependency) is already inside an active request span shaped like the
    one SS-11A's ``PlatformTracingMiddleware`` tags, ``evaluate`` must
    emit ``policy.decision`` as a child of that request span — not as a
    root span, not as a sibling. Audit / trace correlation pipelines
    walk from the request span down to policy decisions and break
    silently if parentage is lost."""
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("gdx_dispatch.request") as request_span:
        request_context = request_span.get_span_context()
        decision = evaluate(principal, "read", resource)

    # decision is unchanged by parent-span presence
    assert decision.allowed is True
    assert decision.reason == ALLOW_SAME_TENANT_ALLOWED_ACTION

    policy_span = _span_by_name(span_exporter, "policy.decision")
    request_exported = _span_by_name(span_exporter, "gdx_dispatch.request")

    # same trace — not a fresh root
    assert policy_span.context.trace_id == request_context.trace_id
    # direct parent is the request span, not None and not some sibling
    assert policy_span.parent is not None
    assert policy_span.parent.span_id == request_context.span_id
    # request span itself is a root (no parent), confirming we were not
    # already inside some unexpected outer scope
    assert request_exported.parent is None


def test_policy_decision_attributes_coexist_with_middleware_tags(span_exporter):
    """SS-11C integration: SS-11A stamps ``gdx.tenant_id`` (+ optional
    ``gdx.acting_on_tenant_id``) on the request span. SS-11B stamps
    ``policy.decision.*`` on the child span. The two attribute
    namespaces must coexist without collision — the request span keeps
    its middleware tags, the policy span keeps its decision tags, and
    neither overwrites the other."""
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("gdx_dispatch.request") as request_span:
        # simulate SS-11A middleware tagging behaviour on the active
        # request span — without importing the middleware, so this
        # test stays a pure integration-style composition check
        request_span.set_attribute("gdx_dispatch.tenant_id", TENANT_ALPHA)
        request_span.set_attribute("gdx_dispatch.acting_on_tenant_id", TENANT_BRAVO)
        evaluate(principal, "read", resource)

    request_exported = _span_by_name(span_exporter, "gdx_dispatch.request")
    policy_exported = _span_by_name(span_exporter, "policy.decision")

    # request span keeps its middleware tags intact — policy evaluation
    # inside the scope did NOT overwrite them
    assert request_exported.attributes["gdx_dispatch.tenant_id"] == TENANT_ALPHA
    assert request_exported.attributes["gdx_dispatch.acting_on_tenant_id"] == TENANT_BRAVO
    # and did NOT leak policy.* attributes onto the request span
    assert "policy.decision.reason" not in request_exported.attributes
    assert "policy.decision.capability_matched" not in request_exported.attributes

    # policy span carries its decision tags — and did NOT inherit or
    # mirror the middleware tags from the parent
    assert policy_exported.attributes["policy.decision.reason"] == (
        ALLOW_SAME_TENANT_ALLOWED_ACTION
    )
    assert policy_exported.attributes["policy.decision.capability_matched"] is True
    assert "gdx_dispatch.tenant_id" not in policy_exported.attributes
    assert "gdx_dispatch.acting_on_tenant_id" not in policy_exported.attributes


# ── SS-11 Slice D: real ASGI request-path proof of lineage ──────────────────


def test_middleware_and_policy_compose_in_real_asgi_request(span_exporter):
    """SS-11 Slice D integration: dispatch a real HTTP request via
    ``starlette.testclient.TestClient`` through ``PlatformTracingMiddleware``
    and a policy-evaluating endpoint. Prove that:

    * exactly one ``gdx.request`` span (the outer request span that
      ``PlatformTracingMiddleware`` tags) and exactly one
      ``policy.decision`` span are emitted for the request,
    * ``policy.decision.parent.span_id`` equals the request span's own
      span id (direct parentage, not sibling, not root),
    * both spans share the same ``trace_id``,
    * middleware attributes (``gdx.tenant_id`` / ``gdx.acting_on_tenant_id``)
      land on the request span and NOT on the policy span,
    * policy attributes (``policy.decision.reason`` /
      ``policy.decision.capability_matched``) land on the policy span and
      NOT on the request span.

    Uses a minimal test-local FastAPI app (not ``gdx_dispatch.app.create_app()``)
    to avoid pulling the full auth / DB / routers surface; the middleware
    under test still executes inside real ASGI request dispatch. The
    outer ``gdx.request`` span is opened by a thin helper middleware —
    in production OTel ASGI auto-instrumentation plays that role, but
    importing it here would install a second global provider and break
    the OTel singleton invariant this module already preserves.
    """
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from gdx_dispatch.core.middleware.tracing import PlatformTracingMiddleware

    class _OuterRequestSpanMiddleware:
        """Pure ASGI middleware — seeds platform ``request.state`` fields
        and opens the outer ``gdx.request`` span.

        Deliberately *not* a ``BaseHTTPMiddleware`` subclass: that class
        runs ``dispatch`` inside an anyio task group, which under some
        starlette/anyio combinations leaves a context-managed OTel span
        holding the ``TestClient`` portal thread past teardown and hangs
        the pytest process (observed under Codex's timeout-scoped replay
        of commit a74a4d9). A pure ASGI middleware closes the span in
        the same task that runs the receive/send loop, so teardown never
        blocks.

        Starlette 1.0 stores ``request.state`` as a plain dict under
        ``scope["state"]`` and wraps it via ``State(dict)`` on access —
        so we seed keys on the dict directly; ``PlatformTracingMiddleware``
        reads them through ``request.state.tenant`` unchanged.
        """

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

    # Starlette stack order: last ``add_middleware`` call ends up outermost.
    # We want ``_OuterRequestSpanMiddleware`` outer (so the ``gdx.request``
    # span exists before ``PlatformTracingMiddleware`` tries to tag it) and
    # ``PlatformTracingMiddleware`` inner.
    app.add_middleware(PlatformTracingMiddleware)
    app.add_middleware(_OuterRequestSpanMiddleware)

    # Context-managed client so Starlette/ASGI lifespan startup+shutdown run
    # cleanly under pytest; without `with`, TestClient's internal portal
    # thread can leave the event loop pinned at teardown and the process
    # hangs past the test body (observed on commit 8484283 under the
    # timeout-scoped rerun; retained even with the pure-ASGI outer
    # middleware above as belt-and-suspenders).
    with TestClient(app) as client:
        response = client.get("/policy-probe")

    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is True
    assert body["reason"] == ALLOW_SAME_TENANT_ALLOWED_ACTION

    request_span = _span_by_name(span_exporter, "gdx_dispatch.request")
    policy_span = _span_by_name(span_exporter, "policy.decision")

    # same trace — policy span is in the same trace as the request span
    assert policy_span.context.trace_id == request_span.context.trace_id
    # direct parentage — policy span's parent IS the request span
    assert policy_span.parent is not None
    assert policy_span.parent.span_id == request_span.context.span_id
    # the request span is the root of this trace — confirms nothing
    # accidentally wrapped the whole thing in a further outer scope
    assert request_span.parent is None

    # middleware attributes land on the request span
    assert request_span.attributes["gdx_dispatch.tenant_id"] == TENANT_ALPHA
    assert request_span.attributes["gdx_dispatch.acting_on_tenant_id"] == TENANT_BRAVO
    # and do NOT leak onto the policy span
    assert "gdx_dispatch.tenant_id" not in policy_span.attributes
    assert "gdx_dispatch.acting_on_tenant_id" not in policy_span.attributes

    # policy attributes land on the policy span
    assert policy_span.attributes["policy.decision.reason"] == (
        ALLOW_SAME_TENANT_ALLOWED_ACTION
    )
    assert policy_span.attributes["policy.decision.capability_matched"] is True
    # and do NOT leak onto the request span
    assert "policy.decision.reason" not in request_span.attributes
    assert "policy.decision.capability_matched" not in request_span.attributes


# ── dataclass invariant: Principal surface unchanged by this slice ───────────


def test_principal_dataclass_has_no_role_field_today():
    """Pins the assumption that motivates the defensive ``getattr``:
    :class:`Principal` does NOT carry a ``role`` field in SS-7 Slice A.
    If a later slice introduces one, this test breaks loudly and the
    optional-attribute logic above needs a second look."""
    principal = _make_principal()
    field_names = {f.name for f in principal.__dataclass_fields__.values()}
    assert "role" not in field_names
    # `replace` should also refuse to set a nonexistent field
    with pytest.raises(TypeError):
        replace(principal, role="admin")  # type: ignore[call-arg]
