from __future__ import annotations

import logging
import os
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenTelemetry — optional, guarded imports
# ---------------------------------------------------------------------------
_otel_available = False
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    _otel_available = True
except ImportError:  # pragma: no cover
    logger.info("opentelemetry-sdk not installed — tracing disabled")

_otel_exporters_available: dict[str, bool] = {}
try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: F401
        OTLPSpanExporter,
    )

    _otel_exporters_available["otlp"] = True
except ImportError:
    logging.getLogger(__name__).exception("<module> caught exception")
    _otel_exporters_available["otlp"] = False

_otel_fastapi_available = False
try:
    from opentelemetry.instrumentation.fastapi import (  # noqa: F401
        FastAPIInstrumentor,
    )

    _otel_fastapi_available = True
except ImportError:
    logging.getLogger(__name__).exception("<module> caught exception")
    pass

_otel_sqlalchemy_available = False
try:
    from opentelemetry.instrumentation.sqlalchemy import (  # noqa: F401
        SQLAlchemyInstrumentor,
    )

    _otel_sqlalchemy_available = True
except ImportError:
    logging.getLogger(__name__).exception("<module> caught exception")
    pass

_otel_redis_available = False
try:
    from opentelemetry.instrumentation.redis import (  # noqa: F401
        RedisInstrumentor,
    )

    _otel_redis_available = True
except ImportError:
    logging.getLogger(__name__).exception("<module> caught exception")
    pass


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    user = event.get("user")
    if isinstance(user, dict):
        user.pop("email", None)
        user.pop("phone", None)
    return event


def init_sentry(dsn: str, env: str) -> None:
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.05,
        before_send=_before_send,
    )


# ---------------------------------------------------------------------------
# OpenTelemetry initialisation
# ---------------------------------------------------------------------------

def init_otel(service_name: str, app: Any | None = None) -> None:
    """Initialise OpenTelemetry tracing.

    Activated only when **both** conditions are true:
    1. ``OTEL_ENABLED=1`` environment variable is set.
    2. The ``opentelemetry-sdk`` package is installed.

    Parameters
    ----------
    service_name:
        Logical service name attached to every span.
    app:
        Optional FastAPI application instance.  When provided and the
        FastAPI instrumentor is available, auto-instruments all routes.
    """
    enabled = os.environ.get("OTEL_ENABLED", "0") == "1"

    if not enabled:
        logger.info("OpenTelemetry disabled (OTEL_ENABLED != 1)")
        return

    if not _otel_available:
        logger.warning(
            "OTEL_ENABLED=1 but opentelemetry-sdk is not installed — skipping"
        )
        return

    # --- Provider -----------------------------------------------------------
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    # --- Exporter -----------------------------------------------------------
    exporter_type = os.environ.get("OTEL_EXPORTER", "console").lower()

    if exporter_type == "otlp" and _otel_exporters_available.get("otlp"):
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        logger.info("OpenTelemetry OTLP exporter → %s", endpoint)
    else:
        exporter = ConsoleSpanExporter()
        logger.info("OpenTelemetry console exporter (set OTEL_EXPORTER=otlp for OTLP)")

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # --- Auto-instrumentation -----------------------------------------------
    if _otel_fastapi_available and app is not None:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("OpenTelemetry: FastAPI auto-instrumented")

    if _otel_sqlalchemy_available:
        SQLAlchemyInstrumentor().instrument()
        logger.info("OpenTelemetry: SQLAlchemy auto-instrumented")

    if _otel_redis_available:
        RedisInstrumentor().instrument()
        logger.info("OpenTelemetry: Redis auto-instrumented")

    logger.info(
        "OpenTelemetry initialised for service=%s exporter=%s",
        service_name,
        exporter_type,
    )
