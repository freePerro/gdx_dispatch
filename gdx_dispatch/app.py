from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pythonjsonlogger.json import JsonFormatter as _JsonFormatter
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from gdx_dispatch.core import observability
from gdx_dispatch.core.error_handler import global_exception_handler
from gdx_dispatch.core.prometheus import prometheus_middleware
from gdx_dispatch.core.prometheus import router as prometheus_router
from gdx_dispatch.core.request_logging import RequestLoggingMiddleware
from gdx_dispatch.core.tenant import TenantMiddleware

# Router wiring fails closed (GDXA-355). Every router used to sit in its own
# try/except that logged the ImportError and bound an empty APIRouter() in its
# place, so a module that would not import in the production image took its
# routes away while /health stayed green and update.sh called the deploy good.
# Now a failed import raises and the app never serves /health: the release
# workflow's /health smoke test refuses to publish that image, and a failure
# that shows only in the production env leaves update.sh's health gate
# unanswered (it calls a restarting container "may still be migrating", so
# read docker logs for router_import_failed). Only a surface on _OPTIONAL_ROUTERS keeps the logged
# fallback; each one that fires is recorded in ROUTER_FALLBACKS and named by
# /health. tests/test_app_router_wiring_fails_closed.py keeps the old pattern out.
# Empty on purpose: no router is optional today. WeasyPrint looked like the
# candidate, but routers/payments.py imports routers/portal.py, which imports
# routers/pdf.py and so core/pdf_generator.py, so an image without WeasyPrint
# loses payments too (checked 2026-10-07). An entry maps "<module>.<attr>" to
# the empty router served in its place.
_OPTIONAL_ROUTERS: dict[str, APIRouter] = {}
ROUTER_FALLBACKS: list[str] = []


def _load_router(module: str, attr: str) -> Any:
    """Return what ``from <module> import <attr>`` binds, or raise.

    A surface named on ``_OPTIONAL_ROUTERS`` gets its empty fallback router
    instead, and the failure is recorded in ``ROUTER_FALLBACKS``.
    """
    import importlib

    dotted = f"{module}.{attr}"
    try:
        mod = importlib.import_module(module)
        try:
            return getattr(mod, attr)
        except AttributeError:
            # `from pkg import sub` imports the submodule when the package
            # does not already bind the name; do the same.
            return importlib.import_module(dotted)
    except Exception:
        if dotted not in _OPTIONAL_ROUTERS:
            logging.getLogger("gdx_dispatch.app").critical(
                "router_import_failed: %s (refusing to boot without it)", dotted, exc_info=True,
            )
            raise
        logging.getLogger("gdx_dispatch.app").exception("optional_router_unavailable: %s", dotted)
        ROUTER_FALLBACKS.append(dotted)
        return _OPTIONAL_ROUTERS[dotted]


auth = _load_router("gdx_dispatch.routers", "auth")


jobs = _load_router("gdx_dispatch.routers", "jobs")

job_diagnosis_router = _load_router("gdx_dispatch.routers", "job_diagnosis")

job_hazards_receipts_router = _load_router("gdx_dispatch.routers", "job_hazards_receipts")

tech_locations_router = _load_router("gdx_dispatch.routers", "tech_locations")

vehicle_inspections_router = _load_router("gdx_dispatch.routers", "vehicle_inspections")

me_settings_router = _load_router("gdx_dispatch.routers", "me_settings")

estimates = _load_router("gdx_dispatch.routers", "estimates")

technicians = _load_router("gdx_dispatch.routers", "technicians")

stripe_webhook = _load_router("gdx_dispatch.routers", "stripe_webhook")

audit_router = _load_router("gdx_dispatch.routers", "audit")

payments_gdx_router = _load_router("gdx_dispatch.routers", "payments")

expenses_router = _load_router("gdx_dispatch.routers", "expenses")

customers_router = _load_router("gdx_dispatch.routers", "customers")

customer_statements_router = _load_router("gdx_dispatch.routers", "customer_statements")

segments_router = _load_router("gdx_dispatch.routers", "segments")

invoices_router = _load_router("gdx_dispatch.routers", "invoices")

documents_router = _load_router("gdx_dispatch.routers", "documents")

uploads_router = _load_router("gdx_dispatch.routers", "uploads")

pdf_router = _load_router("gdx_dispatch.routers", "pdf")

mobile_router = _load_router("gdx_dispatch.routers", "mobile")

mobile_quoting_router = _load_router("gdx_dispatch.routers", "mobile_quoting")

mobile_invoicing_router = _load_router("gdx_dispatch.routers", "mobile_invoicing")

mobile_day_summary_router = _load_router("gdx_dispatch.routers", "mobile_day_summary")

mobile_chat_router = _load_router("gdx_dispatch.routers", "mobile_chat")

reports_router = _load_router("gdx_dispatch.routers", "reports")

labor_router = _load_router("gdx_dispatch.routers", "labor")

tech_efficiency_router = _load_router("gdx_dispatch.routers", "tech_efficiency")

budgets_router = _load_router("gdx_dispatch.routers", "budgets")

overhead_router = _load_router("gdx_dispatch.routers", "overhead")

warranties_router = _load_router("gdx_dispatch.routers", "warranties")

catalog_router = _load_router("gdx_dispatch.routers", "catalog")

inventory_router = _load_router("gdx_dispatch.routers", "inventory")

vendors_router = _load_router("gdx_dispatch.routers", "vendors")

purchase_orders_router = _load_router("gdx_dispatch.routers", "purchase_orders")

change_orders_router = _load_router("gdx_dispatch.routers", "change_orders")

maintenance_router = _load_router("gdx_dispatch.routers", "maintenance")

gdpr_router = _load_router("gdx_dispatch.routers", "gdpr")

collections_router = _load_router("gdx_dispatch.routers", "collections")

invoice_reminders_router = _load_router("gdx_dispatch.routers", "invoice_reminders")

outbound_emails_router = _load_router("gdx_dispatch.routers", "outbound_emails")

tasks_router = _load_router("gdx_dispatch.routers", "tasks")

appointments_router = _load_router("gdx_dispatch.routers", "appointments")

gps_router = _load_router("gdx_dispatch.routers", "gps")

leads_router = _load_router("gdx_dispatch.routers", "leads")

scheduling_router = _load_router("gdx_dispatch.routers", "scheduling")

payroll_router = _load_router("gdx_dispatch.routers", "payroll")

exports_router = _load_router("gdx_dispatch.routers", "exports")

job_costing_router = _load_router("gdx_dispatch.routers", "job_costing")

onboarding_router = _load_router("gdx_dispatch.routers", "onboarding")

tours_router = _load_router("gdx_dispatch.routers", "tours")

ux_telemetry_router = _load_router("gdx_dispatch.routers", "ux_telemetry")

service_agreements_router = _load_router("gdx_dispatch.routers", "service_agreements")

winback_router = _load_router("gdx_dispatch.routers", "winback")

notes_router = _load_router("gdx_dispatch.routers", "notes")

messages_router = _load_router("gdx_dispatch.routers", "messages")

signatures_router = _load_router("gdx_dispatch.routers", "signatures")

inbound_comms_router = _load_router("gdx_dispatch.routers", "inbound_comms")

cell_gateway_router = _load_router("gdx_dispatch.routers", "cell_gateway")

surveys_router = _load_router("gdx_dispatch.routers", "surveys")

photos_router = _load_router("gdx_dispatch.routers", "photos")

tags_router_module = _load_router("gdx_dispatch.routers", "tags")

games_router = _load_router("gdx_dispatch.routers", "games")

activity_router = _load_router("gdx_dispatch.routers", "activity")

webhooks_router = _load_router("gdx_dispatch.routers", "webhooks")

role_permissions_router = _load_router("gdx_dispatch.routers", "role_permissions")

pricing_router = _load_router("gdx_dispatch.routers", "pricing")

loyalty_router = _load_router("gdx_dispatch.routers", "loyalty")

marketing_router = _load_router("gdx_dispatch.routers", "marketing")

branding_public_router = _load_router("gdx_dispatch.routers", "branding_public")

settings_router = _load_router("gdx_dispatch.routers", "settings")

admin_settings_router = _load_router("gdx_dispatch.routers", "admin_settings")

maps_router = _load_router("gdx_dispatch.routers", "maps")

notifications_router = _load_router("gdx_dispatch.routers", "notifications")

timeclock_router = _load_router("gdx_dispatch.routers", "timeclock")

time_off_router = _load_router("gdx_dispatch.routers", "time_off")

checklists_router = _load_router("gdx_dispatch.routers", "checklists")

ui_compat_router = _load_router("gdx_dispatch.routers", "ui_compat")

sub_resources_router = _load_router("gdx_dispatch.routers", "sub_resources")

reviews_router = _load_router("gdx_dispatch.routers", "reviews")

referrals_router = _load_router("gdx_dispatch.routers", "referrals")

search_router = _load_router("gdx_dispatch.routers", "search")

users_router = _load_router("gdx_dispatch.routers", "users")

# Forecasting module — /api/forecast/* + /api/quickbooks/recurring-transactions.
forecasting_router = _load_router("gdx_dispatch.modules.forecasting", "router")

# GL ledger (S4.5) — /api/accounting/* Accounting Settings (CoA + config store).
ledger_router = _load_router("gdx_dispatch.modules.ledger", "router")

# Bank feeds (Banno Consumer API) — /api/bank-feeds/*.
bank_feeds_router = _load_router("gdx_dispatch.modules.bank_feeds", "router")

quickbooks = _load_router("gdx_dispatch.modules.quickbooks", "qb_router")

timeclock = _load_router("gdx_dispatch.modules.timeclock", "router")

workflows = _load_router("gdx_dispatch.modules.workflows", "router")

proposals = _load_router("gdx_dispatch.modules.proposals", "router")

# modules/gps_dispatch/router.py left 2026-09-10 (#637): its two routes,
# POST /api/dispatch/location and /api/dispatch/routes, had no caller and took
# the technician id from the request body. Tech GPS is POST /api/mobile/location.

customer_portal_router = _load_router("gdx_dispatch.routers", "portal")

custom_fields_router = _load_router("gdx_dispatch.routers", "custom_fields")

webhook_monitor = _load_router("gdx_dispatch.core.webhooks", "monitor")

pwa_router = _load_router("gdx_dispatch.core.pwa", "PWARouter")

dealer_order_router = _load_router("gdx_dispatch.modules.distributor.order_portal", "dealer_router")
distributor_order_router = _load_router("gdx_dispatch.modules.distributor.order_portal", "distributor_router")

jwks_router = _load_router("gdx_dispatch.core.jwks", "JWKSRouter")

core_onboarding_router = _load_router("gdx_dispatch.core.onboarding", "router")

admin_ops_read_router = _load_router("gdx_dispatch.routers.admin_ops", "read_router")
admin_ops_router = _load_router("gdx_dispatch.routers.admin_ops", "router")

admin_db_router = _load_router("gdx_dispatch.routers.admin_db", "router")

integrations_router = _load_router("gdx_dispatch.core.integrations", "router")

push_router = _load_router("gdx_dispatch.core.push_notifications", "router")

payments_public_router = _load_router("gdx_dispatch.core.payments", "public_router")
payments_router = _load_router("gdx_dispatch.core.payments", "router")

from gdx_dispatch.core.api_keys import APIKeyMiddleware  # noqa: E402

api_keys_router = _load_router("gdx_dispatch.core.api_keys", "router")

resources_router = _load_router("gdx_dispatch.routers.resources", "router")

locations_router = _load_router("gdx_dispatch.core.locations", "router")

public_v1_router = _load_router("gdx_dispatch.api.public_router", "router")

ai_comms_router = _load_router("gdx_dispatch.routers.ai_communication", "router")

ai_estimates_router = _load_router("gdx_dispatch.routers.ai_estimates", "router")

door_catalog_router = _load_router("gdx_dispatch.routers.door_catalog", "router")

install_sheet_router = _load_router("gdx_dispatch.routers.install_sheet", "router")

planner_router_mod = _load_router("gdx_dispatch.routers.planner", "router")

audit_dashboard_router = _load_router("gdx_dispatch.core.audit_dashboard", "router")


recommendation_routes_router = _load_router("gdx_dispatch.core.recommendation_routes", "router")

ai_quote_router = _load_router("gdx_dispatch.core.ai_quote", "router")

ai_router_router = _load_router("gdx_dispatch.core.ai_router", "router")

ai_usage_router = _load_router("gdx_dispatch.core.ai_usage_logger", "router")

from gdx_dispatch.core.performance import SlowEndpointMiddleware, SlowQueryMiddleware  # noqa: E402

performance_router = _load_router("gdx_dispatch.core.performance", "router")

security_log_router = _load_router("gdx_dispatch.core.security_logger", "router")

webhook_delivery_log_router = _load_router("gdx_dispatch.core.webhook_logger", "router")

from gdx_dispatch.core.data_access_logger import GDPRDataAccessMiddleware  # noqa: E402

gdpr_access_router = _load_router("gdx_dispatch.core.data_access_logger", "router")

from gdx_dispatch.core.audit_middleware import AuditMiddleware  # noqa: E402
from gdx_dispatch.core.rate_limiter import TenantRateLimitMiddleware as _TenantRateLimitMiddleware  # noqa: E402

dispatch_ws_router = _load_router("gdx_dispatch.routers.dispatch_ws", "router")

parts_pricing_router = _load_router("gdx_dispatch.core.parts_pricing", "router")

van_inventory_router = _load_router("gdx_dispatch.routers", "van_inventory")

commission_router = _load_router("gdx_dispatch.routers", "commission")

service_triggers_router = _load_router("gdx_dispatch.routers", "service_triggers")

variance_report_router = _load_router("gdx_dispatch.routers", "variance_report")

warranty_claims_router = _load_router("gdx_dispatch.routers", "warranty_claims")

safety_checklist_router = _load_router("gdx_dispatch.routers", "safety_checklist")

# estimate_nurture removed 2026-09-19 (silent-success sweep): its /run
# endpoint counted "sent" nurture emails while sending nothing, and no UI,
# beat schedule or task ever called any of its four routes. The
# EstimateNurtureRule/Log tables stay (a drop is its own migration+ruling);
# note run's fake log rows would suppress a real send via the already-sent
# dedup if the feature is ever rebuilt — clear the log table first.

user_performance_router = _load_router("gdx_dispatch.routers", "performance")

email_settings_router = _load_router("gdx_dispatch.routers", "email_settings")

parts_needed_router = _load_router("gdx_dispatch.routers", "parts_needed")

job_assignments_router = _load_router("gdx_dispatch.routers", "job_assignments")

job_visits_router = _load_router("gdx_dispatch.routers", "job_visits")

push_v2_router = _load_router("gdx_dispatch.routers", "push")

holding_areas_router = _load_router("gdx_dispatch.routers", "holding_areas")

service_calls_router = _load_router("gdx_dispatch.routers", "service_calls")

bug_reports_router = _load_router("gdx_dispatch.routers", "bug_reports")

support_router = _load_router("gdx_dispatch.routers", "support")

instant_estimate_router = _load_router("gdx_dispatch.routers", "instant_estimate")

pdf_templates_router = _load_router("gdx_dispatch.routers", "pdf_templates")

# circuit_breaker module-level instances are imported here so they are
# initialised at startup; routes can import them directly from gdx_dispatch.core.circuit_breaker.
from gdx_dispatch.core.circuit_breaker import (  # noqa: E402,F401 – side-effect import
    email_circuit,
    qb_circuit,
    stripe_circuit,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # CSP covers the legitimate third-party resources the app actually uses:
        # - CloudFlare Insights beacon auto-injected on HTML responses by CF
        # - The email composers render the outgoing PDF in a blob: iframe
        #   (ComposerPdfPreview.vue) — without frame-src, framing falls back to
        #   default-src and the preview would break the day this is enforced
        response.headers["Content-Security-Policy-Report-Only"] = (
            "default-src 'self'; "
            "script-src 'self' https://static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "worker-src 'self'; "
            "frame-src 'self' blob:; "
            "connect-src 'self' https://static.cloudflareinsights.com https://cloudflareinsights.com"
        )
        return response


class _DefaultLogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        if not hasattr(record, "tenant_id"):
            record.tenant_id = "-"
        return True


def configure_json_logging(level: str | None = None, stream: Any | None = None) -> None:
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    root.setLevel(log_level)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        _JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s %(request_id)s %(tenant_id)s"
        )
    )
    handler.addFilter(_DefaultLogContextFilter())
    root.handlers = [handler]


# The app-wide slowapi default. A PLAIN STRING on purpose — never a callable.
#
# This replaced `_tier_limit`, a callable that tried to read `x-tenant-tier`
# off the request and return 600/minute for "professional", else 120/minute.
# It could never do that. slowapi calls a `default_limits` provider with NO
# arguments unless its signature has a parameter literally named `key` — and
# then it passes the *key* (here, the client IP), not the request
# (`slowapi/wrappers.py`):
#
#     if callable(self.__limit_provider):
#         if "key" in inspect.signature(self.__limit_provider).parameters:
#             limit_raw = self.__limit_provider(self.key_function(self.request))
#         else:
#             limit_raw = self.__limit_provider()
#
# `_tier_limit(*args, **kwargs)` had no `key` parameter, so it was always
# called as `_tier_limit()`, `request` was always None, and it always returned
# "120/minute". Measured, not assumed: 120 requests then 429, identically with
# and without `x-tenant-tier: professional`.
#
# So this constant is not a behaviour change — it is what the app already did.
# It is a string so that a request-dependent limit cannot be reintroduced by
# accident; `tests/test_rate_limit_default_is_static.py` fails if it is.
#
# The same dead branch also held an E2E bypass (GDX_E2E_BYPASS + x-e2e-test →
# 100000/minute) which likewise never fired — verified: e2e traffic still 429s
# at 120. It needed a different mechanism than a default_limits callable:
# create_app() now sets `limiter.enabled` from GDX_E2E_BYPASS (#579). The
# bypass in `core/rate_limiter.py` is a separate layer and is unchanged.
DEFAULT_RATE_LIMIT = "120/minute"

limiter = Limiter(key_func=get_remote_address, default_limits=[DEFAULT_RATE_LIMIT])
# What slowapi itself decided at construction: it reads RATELIMIT_ENABLED from the
# environment (slowapi 0.1.10 extension.py:235). create_app() combines this with
# the E2E bypass rather than overwriting it, so that switch keeps working.
_LIMITER_CONFIGURED_ENABLED = limiter.enabled


def _check_customer_facing_config() -> None:
    """Fail loud at startup when env vars that drive customer-facing signals
    are missing in prod. The cost of a missing welcome email is a user who
    can't reach their account (Becky 2026-04-30: lost a full day to
    PLATFORM_SMTP_PASS being unset). Logs a structured warning
    rather than refusing to start, so a single missing var doesn't take down
    the whole platform — but the warning is loud, audit-able, and visible
    in `docker logs` immediately.
    """
    # Default to "be loud about missing config" when GDX_ENV is unset or
    # unknown. Only suppress in environments that are explicitly dev/test —
    # prod is currently running with GDX_ENV empty (2026-05-01 audit).
    env = os.environ.get("GDX_ENV", "").lower()
    if env in ("dev", "development", "test", "testing", "local"):
        return
    log = logging.getLogger("gdx_dispatch.app.startup_config")
    # (CLOUDFLARE_API_TOKEN / CLOUDFLARE_ZONE_ID were listed here for "tenant
    # subdomains" — a multi-tenant DNS feature this app never ships. Removed
    # 2026-09-03; they fired an ERROR on every boot for nothing.)
    required_at_startup = [
        ("PLATFORM_SMTP_PASS", "auth emails (welcome / reset) will fail (Becky 2026-04-30 incident)"),
    ]
    missing = [(name, why) for name, why in required_at_startup if not os.environ.get(name)]
    if missing:
        for name, why in missing:
            log.error("STARTUP_CONFIG_MISSING var=%s impact=%s env=%s", name, why, env)
        # Don't refuse to start — admin/dashboard/MCP traffic still works
        # without these. But the error is now plainly visible in container
        # logs and the error sink, instead of buried in a per-request warning that
        # only fires when a real customer is trying to sign up.


def _check_encryption_at_rest() -> None:
    """S122-1 (T1): refuse to boot in production when MASTER_ENCRYPTION_KEY
    is unset. ``gdx_dispatch.core.pii._FERNET`` falls back to ``None`` when the
    key is missing, which makes the QB OAuth token-store helpers round-trip
    plaintext. Columns named ``*_enc`` would then hold cleartext
    credentials. The fallback is intentional for dev/test
    (``test_01_gdx_scaffold.py:303`` pins the contract); the gate fires
    only when GDX_ENV is production-ish.

    Live consumer covered by this gate (post S122-1c, every
    ``EncryptedString`` model has been retyped to plain ``Text``):
      * ``qb_token_store.access_token_enc`` / ``refresh_token_enc``
        (manual ``_encrypt`` helpers, ``gdx_dispatch.modules.quickbooks.oauth``)

    ``tenants.db_url_enc`` used to be the second consumer. The column is not
    in ``migrations/baseline_squashed.sql``; the identity decrypt shim and the
    ``tools/`` scripts that still SELECTed the column were removed 2026-09-03,
    so this gate has no DB-URL ciphertext left to protect.
    (An earlier version of this note cited "migrations 081-083" — copied from
    ``core/database.py`` and wrong: this tree's migrations stop at 062.)

    NOT protected here: ``Customer.{name,email,phone,address}``,
    ``webhook_endpoints.secret``, ``integration_configs.secret``. Those
    are plain ``Text`` post-S122-1c — typing matches the actual stored
    plaintext bytes.
    """
    from gdx_dispatch.core import pii  # noqa: PLC0415 — module-load triggers _FERNET init
    env = os.environ.get("GDX_ENV", "").lower()
    # pytest sets PYTEST_CURRENT_TEST on every test; never refuse-to-boot under it.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    is_prod = env in ("", "prod", "production")  # unset GDX_ENV in prod today
    status = pii.encryption_status()
    log = logging.getLogger("gdx_dispatch.app.startup_encryption")
    if status.scan_error is not None:
        # An attestation scan that fails silently is the false-negative
        # surface this helper exists to close. Refuse to boot in prod;
        # warn loudly in dev so the developer notices the broken import.
        log.error(
            "STARTUP_ENCRYPTION_SCAN_ERROR err=%s env=%s",
            status.scan_error, env or "<unset>",
        )
        if is_prod:
            raise SystemExit(
                f"REFUSING TO BOOT: pii.encryption_status() scan failed: {status.scan_error}. "
                "An EncryptedString column may exist on an unimported base. "
                "Fix the import error or override with GDX_ENV=dev."
            )
    if status.key_loaded:
        # Key is loaded — the manual _encrypt paths work. If any
        # EncryptedString columns also exist, log them so the boot
        # record makes the coverage explicit (auditor packet artifact).
        # Also log a salt-fingerprint of TENANT_ID + the first 6 chars
        # of the HKDF-derived keyring's own URL-safe base64 representation
        # so cross-container divergence (auditor round-5 finding) shows
        # up immediately: every container with the same MASTER_ENCRYPTION_KEY
        # and TENANT_ID must log identical fingerprints. Bytes that derive
        # the key are never logged.
        import hashlib  # noqa: PLC0415
        tenant_salt = os.environ.get("TENANT_ID", "")
        salt_fp = hashlib.sha256(tenant_salt.encode()).hexdigest()[:8]
        log.info(
            "STARTUP_ENCRYPTION_OK key=loaded columns=%d (%s) tenant_salt_fp=%s",
            len(status.columns),
            ",".join(f"{c.plane}.{c.table}.{c.column}" for c in status.columns),
            salt_fp,
        )
        return
    if is_prod:
        log.error(
            "STARTUP_ENCRYPTION_MISSING var=MASTER_ENCRYPTION_KEY env=%s "
            "impact=qb_token_store_and_db_url_enc_would_be_plaintext "
            "encrypted_string_columns=%d",
            env or "<unset>",
            len(status.columns),
        )
        raise SystemExit(
            "REFUSING TO BOOT: MASTER_ENCRYPTION_KEY is unset in a production-like "
            "environment (GDX_ENV=%s). Encrypted columns (qb_token_store.*_enc, "
            "tenants.db_url_enc) would silently store plaintext. Set "
            "MASTER_ENCRYPTION_KEY or override with GDX_ENV=dev." % (env or "<unset>")
        )
    log.warning(
        "STARTUP_ENCRYPTION_DEV_MODE MASTER_ENCRYPTION_KEY is unset; "
        "EncryptedString columns will round-trip plaintext "
        "(declared=%d). OK in dev/test; would refuse to boot in prod.",
        len(status.columns),
    )


def _check_audit_guard() -> None:
    """Say at boot whether audit_logs carries its immutability guard (GDXA-352).

    Read-only and never fatal: a database without the guard has been serving
    that way, and refusing to boot over it would take prod down to fix a
    hardening gap. Postgres only — on SQLite ``ensure_audit_table`` installs the
    triggers itself on first use, with no role to refuse it.
    """
    log = logging.getLogger("gdx_dispatch.app.startup_audit_guard")
    try:
        from gdx_dispatch.core.audit import audit_guard_present
        from gdx_dispatch.core.database import engine

        if engine.dialect.name != "postgresql":
            return
        with engine.connect() as conn:
            present = audit_guard_present(conn)
    except Exception:  # noqa: BLE001
        log.exception("STARTUP_AUDIT_GUARD_CHECK_FAILED")
        return
    if present:
        log.info("STARTUP_AUDIT_GUARD_OK")
        return
    log.error(
        "STARTUP_AUDIT_GUARD_MISSING audit_logs has no audit_logs_immutable_guard "
        "triggers — a raw UPDATE/DELETE on the audit trail would succeed "
        "(ARCHITECTURAL INVARIANT #2). Install it as the table owner: migration 107."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    observability.init_otel(service_name="gdx-api", app=app)
    _check_encryption_at_rest()
    _check_customer_facing_config()
    _check_audit_guard()
    # The plugin-host internal token is normally DERIVED from SECRET_KEY, so a
    # container that disagrees breaks plugin events, the restart hook, the
    # credential store and the browser stream at once — while every container
    # still looks healthy. Log the fingerprint so it can be compared with
    # plugin-host's line instead of guessed at (#596).
    from gdx_dispatch.core.internal_auth import log_identity

    log_identity(logging.getLogger("gdx_dispatch.app.startup_internal_auth"), "app")
    # GDXA-271: marked alarm checks → error sink + hourly-deduped email.
    # No-op unless OPS_ALERT_EMAIL is set; the celery side installs it in
    # worker_process_init (core/celery_app.py).
    from gdx_dispatch.modules.error_sink.ops_alert import install_ops_alert_handler

    install_ops_alert_handler()
    # Sprint Outlook Integration: seed GDX outlook credentials from env
    # if the existing POWER_APPS_*/GDX_MICROSOFT_SECRET_KEY are set.
    # Idempotent + swallow-all-errors per bootstrap contract.
    try:
        from gdx_dispatch.modules.outlook.bootstrap import run_outlook_bootstrap_safely
        result = run_outlook_bootstrap_safely()
        if result.get("seeded"):
            logging.getLogger("gdx_dispatch.app").info(
                "outlook bootstrap: seeded %s", result.get("fields", []),
            )
    except Exception:  # noqa: BLE001
        logging.getLogger("gdx_dispatch.app").exception("outlook bootstrap failed at startup")
    # MCP Streamable-HTTP transport (Sprint mcp-streamable-http S2):
    # FastMCP needs its own lifespan to start its session-manager task
    # group; without this, every /mcp request 500s with
    # "Task group is not initialized". mount_mcp() stashes the
    # sub-app on app.state.mcp_subapp during create_app().
    from gdx_dispatch.core.mcp_mount import mcp_subapp_lifespan
    async with mcp_subapp_lifespan(app):
        yield


def create_app() -> FastAPI:
    configure_json_logging()
    # GL chokepoint tripwire (S4): catches Invoice.status writes that bypass
    # transition_invoice_status once ledger posting is enabled. No-op while
    # the flag is off. Idempotent, process-global.
    from gdx_dispatch.modules.ledger.guard import install_flush_guard

    install_flush_guard()
    # Arm the webhook after_commit dispatch hook: domain events staged during a
    # business transaction are enqueued for delivery once that txn commits.
    from gdx_dispatch.core.webhooks.emit import install_webhook_dispatch_hook

    install_webhook_dispatch_hook()
    app = FastAPI(
        title="GDX API",
        description="GDX Dispatch — field service dispatch API (single-tenant). "
                    "Manages jobs, customers, estimates, invoices, technician dispatch, "
                    "and AI-powered quoting for a garage door service company.",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    # The slowapi layer's E2E bypass (#579). The old one lived in a
    # default_limits callable, which slowapi never hands the request, so it
    # could not fire and e2e traffic was capped at 120/minute (see
    # DEFAULT_RATE_LIMIT). slowapi 0.1.10 checks `limiter.enabled` on every
    # request, in SlowAPIMiddleware.dispatch and on the per-route decorator
    # path (middleware.py:123, extension.py:574), so this switches the whole
    # layer off.
    #
    # Coarser than the old intent: the env var alone turns it off, with no
    # x-e2e-test header required. That header is client-supplied, so it never
    # guarded anything; the env var is the whole protection. GDX_E2E_BYPASS=1
    # belongs to throwaway e2e containers and the lab stack. Prod runs 0, and
    # the celery workers and demo leave it unset (checked live 2026-09-14).
    #
    # `limiter` is module-global, so this assigns BOTH ways: the most recent
    # create_app() in a process decides for every app in it. Setting only False
    # would leave slowapi off for apps built later in the same process. It
    # starts from _LIMITER_CONFIGURED_ENABLED, so RATELIMIT_ENABLED=false still
    # turns slowapi off.
    limiter.enabled = _LIMITER_CONFIGURED_ENABLED and os.environ.get("GDX_E2E_BYPASS") != "1"
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(Exception, global_exception_handler)
    app.add_exception_handler(IntegrityError, global_exception_handler)
    app.add_exception_handler(ValueError, global_exception_handler)
    app.add_exception_handler(HTTPException, global_exception_handler)
    app.middleware("http")(prometheus_middleware)
    app.add_middleware(SlowAPIMiddleware)
    # Explicit allowlist only. Credentialed CORS must NOT be combined with a
    # wildcard/reflective origin regex (any taken-over subdomain could then make
    # authenticated cross-origin reads). Operators set the app's public origin(s)
    # via GDX_PUBLIC_BASE_URL (comma-separated for multiple). Empty by default →
    # no cross-origin access, which is correct when the SPA is served same-origin.
    _cors_origins = [
        o.strip().rstrip("/")
        for o in os.getenv("GDX_PUBLIC_BASE_URL", "").split(",")
        if o.strip()
    ]
    # In dev, also allow the Vite dev server on localhost.
    if os.getenv("APP_VERSION", "") == "dev":
        _cors_origins.extend(["http://localhost:5173", "http://localhost:3000"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    try:
        from gdx_dispatch.core.error_handler import ErrorHandlerMiddleware
        app.add_middleware(ErrorHandlerMiddleware)
    except ImportError:
        logging.getLogger("gdx_dispatch.app").exception("error_handler_middleware_unavailable")
    app.add_middleware(SlowQueryMiddleware)
    app.add_middleware(SlowEndpointMiddleware)
    app.add_middleware(GDPRDataAccessMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(APIKeyMiddleware)
    # ServiceKeyMiddleware (X-Service-Key / svc_live_ keys) was REMOVED
    # 2026-08-12. It granted admin-equivalent access but shipped with no way
    # to provision a key — no web UI, and the CLI its own docstring pointed at
    # was never written — so the feature could only ever be used by hand-editing
    # the DB. Nothing in the app sent the header. It was also the only producer
    # of service-account identity, which is why the `actor_kind ==
    # "service_account"` branch in routers/auth/core.py now fails closed.
    # Its ORM model was deleted 2026-09-03 with the SaaS-residue purge and
    # migration 087 dropped the empty `service_accounts` table.
    app.add_middleware(_TenantRateLimitMiddleware)
    try:
        from gdx_dispatch.core.middleware.tracing import PlatformTracingMiddleware
        app.add_middleware(PlatformTracingMiddleware)
    except ImportError:
        logging.getLogger("gdx_dispatch.app").exception("platform_tracing_middleware_unavailable")

    # -----------------------------------------------------------------
    # Sprint 0.9-m: SS-14..35 middleware stack.
    #
    # Starlette semantics: the LAST ``add_middleware`` call wraps OUTERMOST.
    # Actual request flow through this block (outermost → innermost):
    #     TenantMiddleware → APIVersioningMiddleware → IdempotencyMiddleware
    #     → handler
    # Written below in reverse (innermost first) so the LAST line (Tenant)
    # ends up outermost. Middlewares registered ABOVE this block
    # (AuditMiddleware, APIKeyMiddleware, _TenantRateLimitMiddleware,
    # PlatformTracingMiddleware) sit inside it.
    #
    # The original 0.9-m design also routed through TenantRoleMiddleware,
    # ConsumerAuditMiddleware and CrossTenantAccessMiddleware. None of the
    # three exist any more — they went with the multi-tenant platform
    # schema (see the SS-28 removal note below).
    #
    # AuthMiddleware: the Sprint 0.9-d composite auth dispatcher is plumbed
    # as ``Depends(get_current_principal)`` per-route, not as a Starlette
    # middleware — so no middleware registration line is needed for it.
    # -----------------------------------------------------------------

    # SS-14 Idempotency-Key replay cache (innermost — closest to handler).
    try:
        from gdx_dispatch.core.middleware.idempotency import IdempotencyMiddleware as _SS14IdempotencyMiddleware
        from gdx_dispatch.routers.auth import _denylist_redis_client as _idempotency_redis_factory

        _idempotency_redis = _idempotency_redis_factory()
        if _idempotency_redis is not None:
            app.add_middleware(_SS14IdempotencyMiddleware, redis_client=_idempotency_redis)
        else:
            logging.getLogger("gdx_dispatch.app").info(
                "ss14_idempotency_middleware_skipped: redis unavailable (falls back to pass-through)"
            )
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("ss14_idempotency_middleware_unavailable")

    # M36 (money audit 2026-08-04): the SS-14 cache above was a permanent
    # pass-through — it requires request.state.principal and nothing in
    # production ever stamped it. This outer middleware (added later = runs
    # FIRST) stamps a minimal verified principal on exactly the requests the
    # cache handles (POST + Idempotency-Key + Bearer), so the replay cache
    # finally functions for the offline queue's replays. It never rejects.
    try:
        from gdx_dispatch.core.middleware.principal_stamp import PrincipalStampMiddleware
        app.add_middleware(PrincipalStampMiddleware)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("principal_stamp_middleware_unavailable")

    # SS-28 Consumer audit log capture — REMOVED. This was a multi-tenant
    # platform (Command Center) middleware that fail-closed-wrote to the
    # platform_consumer_audit table on every request; that table and the rest
    # of the platform schema are gone in this single-tenant release.

    # SS-25 API versioning (Accept header parse + deprecation registry).
    try:
        from gdx_dispatch.core.middleware.api_versioning import APIVersioningMiddleware
        app.add_middleware(APIVersioningMiddleware)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("ss25_api_versioning_middleware_unavailable")

    # TenantMiddleware stays LAST = outermost (sets request.state.tenant
    # before any SS-14..35 middleware inspects it).
    app.add_middleware(TenantMiddleware)

    @app.get("/health")
    def health():
        from fastapi.responses import JSONResponse
        from sqlalchemy import create_engine, text

        def _probe(url: str) -> bool:
            """Return True if a quick SELECT 1 succeeds, False otherwise."""
            try:
                # connect_timeout is a psycopg2 kwarg; sqlite3.Connection
                # rejects it (TypeError) — which made /health permanently 503
                # on any sqlite-backed instance.
                connect_args = {} if url.startswith("sqlite") else {"connect_timeout": 2}
                eng = create_engine(url, connect_args=connect_args)
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
                eng.dispose()
                return True
            except Exception:
                # Health-probe pattern: the False return IS the signal the
                # caller consumes. Not a silent swallow — the information
                # reaches the operator via the probe result.
                logging.getLogger("gdx_dispatch.app").exception("import/init failed")
                return False

        # ── Probe the one database ─
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url and not _probe(db_url):
            return JSONResponse(
                status_code=503,
                content={"status": "down", "db": "error"},
            )

        # ── Probe PgBouncer ─────────────────────────────────────────────
        pgbouncer_url = os.environ.get("PGBOUNCER_URL", "")
        if pgbouncer_url and not _probe(pgbouncer_url):
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "db": "ok", "pgbouncer": "down"},
            )

        # ── Resolved denylist backend visibility (SS-7 Slice L) ─────────
        # Surface the mode that :func:`gdx_dispatch.routers.auth._denylist_redis_client`
        # resolves to so dashboards and on-call can see whether a deployment
        # ended up on Redis fan-out or local-only. Call the helper via a
        # local import to avoid a module-level circular import, and guard
        # with try/except because /health must stay green on any upstream
        # failure (fail-open is the contract for this subsystem).
        #
        # IMPORTANT: never expose REDIS_URL, any connection string, or any
        # credential here — this is a boolean-style read (`memory`/`redis`).
        try:
            from gdx_dispatch.routers.auth import _denylist_redis_client

            denylist_backend = "redis" if _denylist_redis_client() is not None else "memory"
        except Exception:
            logging.getLogger("gdx_dispatch.app").exception("denylist_backend_probe_failed")
            denylist_backend = "memory"

        result: dict[str, str] = {"status": "ok", "db": "ok", "denylist_backend": denylist_backend}
        if pgbouncer_url:
            result["pgbouncer"] = "ok"
        # An allow-listed router that did not import (GDXA-355). The app is up
        # by design without it, so this names the gap rather than failing.
        if ROUTER_FALLBACKS:
            result["router_fallbacks"] = ",".join(ROUTER_FALLBACKS)
        return result

    app.include_router(auth.router if hasattr(auth, "router") else auth)
    app.include_router(jobs.router if hasattr(jobs, "router") else jobs)
    app.include_router(
        job_diagnosis_router.router if hasattr(job_diagnosis_router, "router") else job_diagnosis_router
    )
    app.include_router(
        job_hazards_receipts_router.router if hasattr(job_hazards_receipts_router, "router") else job_hazards_receipts_router
    )
    app.include_router(
        tech_locations_router.router if hasattr(tech_locations_router, "router") else tech_locations_router
    )
    app.include_router(
        vehicle_inspections_router.router if hasattr(vehicle_inspections_router, "router") else vehicle_inspections_router
    )
    app.include_router(
        me_settings_router.router if hasattr(me_settings_router, "router") else me_settings_router
    )
    app.include_router(estimates.router if hasattr(estimates, "router") else estimates)
    # install_sheet declares /api/technicians/daily-loadsheet — must register
    # BEFORE technicians.router (prefix=/api/technicians), whose /{technician_id}
    # would otherwise eat "daily-loadsheet" and return 404.
    app.include_router(install_sheet_router)
    app.include_router(technicians.router if hasattr(technicians, "router") else technicians)
    app.include_router(stripe_webhook.router if hasattr(stripe_webhook, "router") else stripe_webhook)
    app.include_router(audit_router.router if hasattr(audit_router, "router") else audit_router)
    app.include_router(payments_gdx_router.router if hasattr(payments_gdx_router, "router") else payments_gdx_router)
    app.include_router(expenses_router.router if hasattr(expenses_router, "router") else expenses_router)
    app.include_router(customers_router.router if hasattr(customers_router, "router") else customers_router)
    app.include_router(
        customer_statements_router.router
        if hasattr(customer_statements_router, "router")
        else customer_statements_router
    )
    app.include_router(segments_router.router if hasattr(segments_router, "router") else segments_router)
    app.include_router(invoices_router.router if hasattr(invoices_router, "router") else invoices_router)
    app.include_router(uploads_router.router if hasattr(uploads_router, "router") else uploads_router)
    app.include_router(documents_router.router if hasattr(documents_router, "router") else documents_router)
    app.include_router(pdf_router.router if hasattr(pdf_router, "router") else pdf_router)
    app.include_router(mobile_router.router if hasattr(mobile_router, "router") else mobile_router)
    app.include_router(mobile_quoting_router.router if hasattr(mobile_quoting_router, "router") else mobile_quoting_router)
    app.include_router(mobile_invoicing_router.router if hasattr(mobile_invoicing_router, "router") else mobile_invoicing_router)
    app.include_router(mobile_day_summary_router.router if hasattr(mobile_day_summary_router, "router") else mobile_day_summary_router)
    app.include_router(mobile_chat_router.router if hasattr(mobile_chat_router, "router") else mobile_chat_router)
    app.include_router(reports_router.router if hasattr(reports_router, "router") else reports_router)
    app.include_router(labor_router.router if hasattr(labor_router, "router") else labor_router)
    app.include_router(tech_efficiency_router.router if hasattr(tech_efficiency_router, "router") else tech_efficiency_router)
    app.include_router(budgets_router.router if hasattr(budgets_router, "router") else budgets_router)
    app.include_router(overhead_router.router if hasattr(overhead_router, "router") else overhead_router)
    app.include_router(warranties_router.router if hasattr(warranties_router, "router") else warranties_router)
    app.include_router(catalog_router.router if hasattr(catalog_router, "router") else catalog_router)
    app.include_router(inventory_router.router if hasattr(inventory_router, "router") else inventory_router)
    app.include_router(vendors_router.router if hasattr(vendors_router, "router") else vendors_router)
    app.include_router(purchase_orders_router.router if hasattr(purchase_orders_router, "router") else purchase_orders_router)
    app.include_router(change_orders_router.router if hasattr(change_orders_router, "router") else change_orders_router)
    app.include_router(payroll_router.router if hasattr(payroll_router, "router") else payroll_router)
    app.include_router(exports_router.router if hasattr(exports_router, "router") else exports_router)
    app.include_router(maintenance_router.router if hasattr(maintenance_router, "router") else maintenance_router)
    app.include_router(gdpr_router.router if hasattr(gdpr_router, "router") else gdpr_router)
    app.include_router(collections_router.router if hasattr(collections_router, "router") else collections_router)
    app.include_router(invoice_reminders_router.router if hasattr(invoice_reminders_router, "router") else invoice_reminders_router)
    app.include_router(outbound_emails_router.router if hasattr(outbound_emails_router, "router") else outbound_emails_router)
    app.include_router(tasks_router.router if hasattr(tasks_router, "router") else tasks_router)
    app.include_router(appointments_router.router if hasattr(appointments_router, "router") else appointments_router)
    app.include_router(gps_router.router if hasattr(gps_router, "router") else gps_router)
    app.include_router(leads_router.router if hasattr(leads_router, "router") else leads_router)
    app.include_router(scheduling_router.router if hasattr(scheduling_router, "router") else scheduling_router)
    app.include_router(job_costing_router.router if hasattr(job_costing_router, "router") else job_costing_router)
    app.include_router(onboarding_router.router if hasattr(onboarding_router, "router") else onboarding_router)
    app.include_router(tours_router.router if hasattr(tours_router, "router") else tours_router)
    app.include_router(ux_telemetry_router.router if hasattr(ux_telemetry_router, "router") else ux_telemetry_router)
    app.include_router(service_agreements_router.router if hasattr(service_agreements_router, "router") else service_agreements_router)
    app.include_router(winback_router.router if hasattr(winback_router, "router") else winback_router)
    app.include_router(notes_router.router if hasattr(notes_router, "router") else notes_router)
    app.include_router(messages_router.router if hasattr(messages_router, "router") else messages_router)
    # Public routes first so `/api/signatures/token/{token}` matches before the
    # admin `/api/signatures/{document_type}/{document_id}` collision path.
    app.include_router(signatures_router.public_router)
    app.include_router(signatures_router.admin_router)
    app.include_router(inbound_comms_router.public_router)
    app.include_router(inbound_comms_router.admin_router)
    app.include_router(cell_gateway_router.public_router)
    app.include_router(surveys_router.public_router)
    app.include_router(surveys_router.admin_router)
    app.include_router(photos_router.router if hasattr(photos_router, "router") else photos_router)
    app.include_router(tags_router_module.router if hasattr(tags_router_module, "router") else tags_router_module)
    app.include_router(games_router.router if hasattr(games_router, "router") else games_router)
    app.include_router(activity_router.router if hasattr(activity_router, "router") else activity_router)
    app.include_router(webhooks_router.router if hasattr(webhooks_router, "router") else webhooks_router)
    app.include_router(role_permissions_router.router if hasattr(role_permissions_router, "router") else role_permissions_router)
    app.include_router(pricing_router.router if hasattr(pricing_router, "router") else pricing_router)
    # Sprint 1.0.5 — pricing-engine admin endpoints (tier sets + volume discount + preview)
    app.include_router(_load_router("gdx_dispatch.routers", "pricing_admin").router)
    # Sprint S97 — labor pricing matrix admin (size/SKU-keyed flat-rate labor)
    app.include_router(_load_router("gdx_dispatch.routers", "labor_pricing_admin").router)
    # Sprint S97 slice 8 — labor variance (estimated vs actual hours/cost)
    app.include_router(_load_router("gdx_dispatch.routers", "labor_variance").router)
    # Sprint vendor-statement-recon — Midwest statement upload + parse
    app.include_router(_load_router("gdx_dispatch.routers", "vendor_statements").router)
    # Door listings — doors for sale, published to garagedoorxperts.com.
    # `public_router` serves photo bytes unauthenticated and is deliberately
    # NOT under /api: the per-key 60 req/min cap in core/api_keys.py would trip
    # on a page of thumbnails and hand an <img> tag a 429 JSON body.
    _door_listings_router = _load_router("gdx_dispatch.routers", "door_listings")
    app.include_router(_door_listings_router.router)
    app.include_router(_door_listings_router.public_router)
    # Sprint vendor-invoice-intake — supplier bill upload + parse + match/confirm
    app.include_router(_load_router("gdx_dispatch.routers", "vendor_invoices").router)
    app.include_router(loyalty_router.router if hasattr(loyalty_router, "router") else loyalty_router)
    app.include_router(marketing_router.router if hasattr(marketing_router, "router") else marketing_router)
    # Register the public branding router BEFORE the gated settings router
    # so /api/settings/branding GET resolves to the unrestricted handler
    # for non-admin users. FastAPI route lookup is first-match-wins.
    app.include_router(branding_public_router.router if hasattr(branding_public_router, "router") else branding_public_router)
    app.include_router(settings_router.router if hasattr(settings_router, "router") else settings_router)
    app.include_router(admin_settings_router.router if hasattr(admin_settings_router, "router") else admin_settings_router)
    app.include_router(maps_router.router if hasattr(maps_router, "router") else maps_router)
    app.include_router(
        notifications_router.router if hasattr(notifications_router, "router") else notifications_router
    )
    # Equipment and Fleet were retired 2026-09-14 (#683): their routers
    # (modules/equipment, routers/equipment_tracking, routers/fleet,
    # modules/fleet) are gone. The models stay registered in models/__init__.
    app.include_router(timeclock_router.router if hasattr(timeclock_router, "router") else timeclock_router)
    # Time off requests + holiday pay (2026-09-23): same module gate as the
    # timeclock, its own file (the 2026-09-23 time-off and holiday pay plan).
    app.include_router(time_off_router.router if hasattr(time_off_router, "router") else time_off_router)
    app.include_router(checklists_router.router if hasattr(checklists_router, "router") else checklists_router)
    # Sub-resource endpoints (customer opt-out and bulk-tag, job line-items)
    # — real DB-backed implementations replacing shims.
    app.include_router(sub_resources_router.router if hasattr(sub_resources_router, "router") else sub_resources_router)
    # UI compat shim — thin handlers for Vue view endpoints that don't yet
    # have a dedicated router implementation. Returns empty lists / default
    # shapes so the UI renders without errors. MUST be registered AFTER all
    # real routers so that any real endpoint wins on path conflicts.
    app.include_router(ui_compat_router.router if hasattr(ui_compat_router, "router") else ui_compat_router)
    app.include_router(reviews_router.router if hasattr(reviews_router, "router") else reviews_router)
    app.include_router(referrals_router.router if hasattr(referrals_router, "router") else referrals_router)
    app.include_router(search_router.router if hasattr(search_router, "router") else search_router)
    app.include_router(users_router.router if hasattr(users_router, "router") else users_router)
    app.include_router(quickbooks.router if hasattr(quickbooks, "router") else quickbooks)
    app.include_router(forecasting_router.router)
    app.include_router(ledger_router.router)
    app.include_router(bank_feeds_router.router)
    # NOTE: gdx_dispatch/modules/*/router.py (legacy) and gdx_dispatch/routers/*.py (newer) both
    # register some overlapping paths with the same function names. The newer
    # versions are richer and tenant-scoped; the legacy modules have some
    # unique endpoints. Both stay mounted for those — though the last one worth
    # naming, /timeclock/report, has no frontend caller either, so this note is
    # thinner than it reads. The modules/inventory router left entirely
    # 2026-09-07: all four of its routes were unauthenticated and none of them
    # worked (the `parts` catalog has no writer). The paths the modules
    # duplicated (inventory parts list/create + low-stock, campaigns
    # list/create/send, timeclock clock-in/status,
    # dispatch locations) were deleted from the module routers 2026-09-06
    # (#569): FastAPI serves the first registration, so they never ran.
    app.include_router(timeclock.router if hasattr(timeclock, "router") else timeclock)
    app.include_router(workflows.router if hasattr(workflows, "router") else workflows)
    app.include_router(proposals.router if hasattr(proposals, "router") else proposals)
    app.include_router(
        customer_portal_router.router if hasattr(customer_portal_router, "router") else customer_portal_router
    )
    # Staff-side portal management (/api/portal) — real endpoints behind the
    # PortalView screen.
    app.include_router(customer_portal_router.staff_router)
    app.include_router(custom_fields_router.router if hasattr(custom_fields_router, "router") else custom_fields_router)
    app.include_router(webhook_monitor.router if hasattr(webhook_monitor, "router") else webhook_monitor)
    app.include_router(pwa_router)
    app.include_router(jwks_router)
    app.include_router(core_onboarding_router, prefix="/api", tags=["onboarding"])
    app.include_router(admin_ops_router)
    app.include_router(admin_ops_read_router)
    app.include_router(admin_db_router)
    # Third-party plugin proxy: forwards /api/plugins/* to the plugin-host
    # container with the authenticated principal (ADR-013). A plugin-host that
    # is down is a runtime 502 from the proxy, not an import failure here.
    # WS browser-stream proxy (ADR-014) — registered first; it's a websocket
    # route so it won't collide with the HTTP catch-all below.
    app.include_router(_load_router("gdx_dispatch.routers.browser_proxy", "router"))
    app.include_router(_load_router("gdx_dispatch.routers.plugins_proxy", "router"))
    app.include_router(_load_router("gdx_dispatch.routers.admin_plugins", "router"))
    app.include_router(push_router)
    app.include_router(locations_router)
    app.include_router(ai_comms_router)
    app.include_router(ai_estimates_router)
    app.include_router(door_catalog_router)
    app.include_router(planner_router_mod)
    app.include_router(audit_dashboard_router)
    app.include_router(recommendation_routes_router)
    app.include_router(ai_quote_router)
    app.include_router(ai_router_router)
    app.include_router(ai_usage_router)
    app.include_router(performance_router)
    app.include_router(security_log_router)
    app.include_router(webhook_delivery_log_router)
    app.include_router(gdpr_access_router)
    app.include_router(api_keys_router)
    app.include_router(payments_router)
    app.include_router(payments_public_router)
    app.include_router(distributor_order_router)
    app.include_router(dealer_order_router)
    app.include_router(prometheus_router)

    app.include_router(_load_router("gdx_dispatch.routers.auth", "sso").router)

    app.include_router(_load_router("gdx_dispatch.routers", "dispatch_scheduling").router)

    app.include_router(_load_router("gdx_dispatch.routers", "voice").router)

    app.include_router(integrations_router)
    app.include_router(van_inventory_router.router if hasattr(van_inventory_router, "router") else van_inventory_router)
    app.include_router(commission_router.router if hasattr(commission_router, "router") else commission_router)
    app.include_router(service_triggers_router.router if hasattr(service_triggers_router, "router") else service_triggers_router)
    app.include_router(variance_report_router.router if hasattr(variance_report_router, "router") else variance_report_router)
    app.include_router(warranty_claims_router.router if hasattr(warranty_claims_router, "router") else warranty_claims_router)
    app.include_router(safety_checklist_router.router if hasattr(safety_checklist_router, "router") else safety_checklist_router)
    app.include_router(user_performance_router.router if hasattr(user_performance_router, "router") else user_performance_router)
    app.include_router(email_settings_router.router if hasattr(email_settings_router, "router") else email_settings_router)
    app.include_router(parts_needed_router.router if hasattr(parts_needed_router, "router") else parts_needed_router)
    app.include_router(job_assignments_router.router if hasattr(job_assignments_router, "router") else job_assignments_router)
    app.include_router(job_visits_router.router if hasattr(job_visits_router, "router") else job_visits_router)
    app.include_router(push_v2_router.router if hasattr(push_v2_router, "router") else push_v2_router)
    app.include_router(holding_areas_router.router if hasattr(holding_areas_router, "router") else holding_areas_router)
    app.include_router(service_calls_router.router if hasattr(service_calls_router, "router") else service_calls_router)
    app.include_router(bug_reports_router.router if hasattr(bug_reports_router, "router") else bug_reports_router)
    app.include_router(support_router.router if hasattr(support_router, "router") else support_router)
    app.include_router(instant_estimate_router.router if hasattr(instant_estimate_router, "router") else instant_estimate_router)
    app.include_router(pdf_templates_router.router if hasattr(pdf_templates_router, "router") else pdf_templates_router)
    app.include_router(dispatch_ws_router)
    # ai_quote_router is already included above — don't register twice
    # (was causing Duplicate Operation ID warnings for api_quote_history/feedback)
    app.include_router(parts_pricing_router)
    app.include_router(resources_router)
    app.include_router(public_v1_router)

    # Sprint 1.x-S26: admin AI settings router.
    app.include_router(_load_router("gdx_dispatch.routers", "admin_ai_settings").router)

    # Sprint tech_mobile S1-Z4: per-tenant tech-mobile feature settings.
    app.include_router(_load_router("gdx_dispatch.routers", "admin_tech_mobile_settings").router)

    # Sprint tech_mobile S1-A8: customer-alert tag taxonomy CRUD.
    app.include_router(_load_router("gdx_dispatch.routers", "admin_customer_tags").router)

    # Sprint phone-com pc-s8/s9/s10/s11/s12: integration card + ops + webhooks.
    app.include_router(_load_router("gdx_dispatch.routers", "phone_com_settings").router)
    app.include_router(_load_router("gdx_dispatch.modules.phone_com", "router").router)
    app.include_router(_load_router("gdx_dispatch.modules.phone_com", "webhook_router").router)

    # Sprint Outlook Integration: OAuth + read views + send + webhook receiver.
    app.include_router(_load_router("gdx_dispatch.routers", "outlook_oauth").router)
    app.include_router(_load_router("gdx_dispatch.modules.outlook", "views_router").router)
    app.include_router(_load_router("gdx_dispatch.modules.outlook", "send_router").router)
    app.include_router(_load_router("gdx_dispatch.modules.outlook", "webhook_router").router)
    app.include_router(_load_router("gdx_dispatch.modules.outlook", "admin_settings_router").router)
    app.include_router(_load_router("gdx_dispatch.modules.outlook", "folders_router").router)

    # 2026-04-29 — Tax module (default rate per tenant + customer exemptions).
    # Sprint-shaped so jurisdiction lookup, category overrides, and
    # provider plugins (Avalara/TaxJar) can layer in without a refactor.
    app.include_router(_load_router("gdx_dispatch.modules.tax.router", "router"))

    # 2026-04-29 / UX audit F-11 — Numbering module (per-tenant job number
    # format + counter). Same module shape as tax: today only jobs use it,
    # tomorrow estimates/invoices share the same format engine.
    app.include_router(_load_router("gdx_dispatch.modules.numbering.router", "router"))

    # 2026-04-29 / UX audit F-8 — Job workflow flags (per-tenant toggles
    # for schedule lock, arrival event, arrival SMS, complete-time
    # required fields). Default behavior baked into routers/jobs.py.
    app.include_router(_load_router("gdx_dispatch.modules.workflow.router", "router"))

    # 2026-04-29 / UX audit F-18 — Self-hosted error sink (replaces Sentry).
    # Captures 5xx + unhandled exceptions to control-plane server_errors.
    app.include_router(_load_router("gdx_dispatch.modules.error_sink.router", "router"))

    # 2026-04-29 / UX audit F-36 — billing terms (per-tenant payment-terms
    # defaults + per-class overrides + early-pay / late-fee / interest config).
    app.include_router(_load_router("gdx_dispatch.modules.billing_terms.router", "router"))

    # 2026-04-29 / UX audit F-74 — Catalog description policy.
    app.include_router(_load_router("gdx_dispatch.modules.catalog_policy.router", "router"))

    # 2026-04-30 — Estimates feature toggles (per-line margin override, etc.).
    app.include_router(_load_router("gdx_dispatch.modules.estimates_features.router", "router"))

    # 2026-05-01 — Dispatch settings (scheduled-no-tech gates + lane visibility).
    app.include_router(_load_router("gdx_dispatch.modules.dispatch_settings.router", "router"))

    # Session policy — tenant-wide inactivity auto-logout.
    app.include_router(_load_router("gdx_dispatch.routers.session_policy", "router"))

    # 2026-04-29 / UX audit F-82 — Payroll module (true vs estimated cost).
    app.include_router(_load_router("gdx_dispatch.modules.payroll.router", "router"))

    # 2026-04-29 / UX audit F-89 — Maps provider selector.
    app.include_router(_load_router("gdx_dispatch.modules.maps_provider.router", "router"))

    # Sprint 1.x-S14: per-tenant AI assistant skeleton (`/api/ai/ask`).
    app.include_router(_load_router("gdx_dispatch.routers", "ai").router)

    # -----------------------------------------------------------------
    # Sprint 0.9-n: SS-14..35 platform routers.
    # -----------------------------------------------------------------
    # Command Center / SaaS-platform routers were removed for this single-tenant
    # release (their tables are gone from the squashed baseline). Only the
    # app-level metadata endpoints. (The PAT/SCIM identity cluster was
    # removed with the single-tenant cleanup.)
    app.include_router(_load_router("gdx_dispatch.routers.api_metadata", "router"))  # SS-25
    app.include_router(_load_router("gdx_dispatch.routers.well_known", "router"))  # SS-26

    # Universal route reorder: move literal-path routes ahead of
    # parameterized ones so /customers/duplicates doesn't get eaten by
    # /customers/{customer_id}. Applied once, after every router is
    # registered. Fixes ~31 collisions found by static analysis 2026-04-21.
    try:
        from gdx_dispatch.core.route_order import reorder_literal_paths_first
        _moved = reorder_literal_paths_first(app)
        logging.getLogger("gdx_dispatch.app").info(
            "route_order_normalized: moved=%d", _moved
        )
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("route_order_normalize_failed")

    # ── MCP Streamable-HTTP transport ───────────────────────────────────────
    # Sprint mcp-streamable-http S2: mount the FastMCP singleton at /mcp.
    # MUST happen BEFORE the SPA catch-all below, otherwise the catch-all
    # shadows /mcp and serves index.html (the original bug).
    # gdx_dispatch.core.mcp_tools side-effect import populates the registry; the
    # mount function bridges it onto FastMCP and mounts the ASGI sub-app.
    import gdx_dispatch.core.mcp_tools  # noqa: F401  — side-effect: registers tool set
    from gdx_dispatch.core.mcp_mount import mount_mcp
    mount_mcp(app)

    # ── Vue SPA frontend ────────────────────────────────────────────────────
    # Serve built Vue assets and catch-all for client-side routing
    from pathlib import Path as _Path
    _frontend_dist = _Path(__file__).parent / "frontend" / "dist"
    if _frontend_dist.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        # Serve /assets/* (JS, CSS, images)
        _assets_dir = _frontend_dist / "assets"
        if _assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="vue-assets")

        # SPA catch-all: any unmatched GET returns index.html for Vue Router
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str):
            # Don't catch API/doc routes
            if full_path.startswith(("api/", "docs", "redoc", "openapi.json", "health", "mcp", ".well-known")):
                from fastapi.responses import JSONResponse
                return JSONResponse({"error": "not_found"}, status_code=404)
            # Try a real file in dist/ first (sw.js, help-index.json, favicon,
            # manifest, robots.txt, etc.) before falling back to the SPA shell.
            # Without this, any non-asset file 404s and the SPA index.html is
            # served — which the help drawer parses as JSON and crashes.
            if full_path:
                _candidate = _frontend_dist / full_path
                try:
                    if (
                        _candidate.is_file()
                        and _candidate.resolve().is_relative_to(_frontend_dist.resolve())
                    ):
                        return FileResponse(str(_candidate))
                except (ValueError, OSError):
                    pass
            _index = _frontend_dist / "index.html"
            if _index.exists():
                return FileResponse(str(_index))
            from fastapi.responses import HTMLResponse
            return HTMLResponse("<h1>Frontend not built</h1>", status_code=503)
    else:
        logging.getLogger("gdx_dispatch.app").warning("Vue frontend dist/ not found — SPA routes disabled")

    return app


app = create_app()
