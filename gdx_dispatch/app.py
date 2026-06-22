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
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.error_handler import global_exception_handler
from gdx_dispatch.core.prometheus import prometheus_middleware
from gdx_dispatch.core.prometheus import router as prometheus_router
from gdx_dispatch.core.request_logging import RequestLoggingMiddleware
from gdx_dispatch.core.tenant import TenantMiddleware

try:
    from gdx_dispatch.routers import auth
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: auth")
    auth = APIRouter(prefix="/auth", tags=["auth"])


try:
    from gdx_dispatch.routers import jobs
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: jobs")
    jobs = APIRouter(prefix="/jobs", tags=["jobs"])

try:
    from gdx_dispatch.routers import job_diagnosis as job_diagnosis_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: job_diagnosis")
    job_diagnosis_router = APIRouter(tags=["job-diagnosis"])

try:
    from gdx_dispatch.routers import job_hazards_receipts as job_hazards_receipts_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: job_hazards_receipts")
    job_hazards_receipts_router = APIRouter(tags=["job-hazards-receipts"])

try:
    from gdx_dispatch.routers import tech_locations as tech_locations_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: tech_locations")
    tech_locations_router = APIRouter(tags=["tech-locations"])

try:
    from gdx_dispatch.routers import vehicle_inspections as vehicle_inspections_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: vehicle_inspections")
    vehicle_inspections_router = APIRouter(tags=["vehicle-inspections"])

try:
    from gdx_dispatch.routers import me_settings as me_settings_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: me_settings")
    me_settings_router = APIRouter(tags=["me-settings"])

try:
    from gdx_dispatch.routers import estimates
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: estimates")
    estimates = APIRouter(prefix="/api/estimates", tags=["estimates"])

try:
    from gdx_dispatch.routers import technicians
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: technicians")
    technicians = APIRouter(prefix="/api/technicians", tags=["technicians"])

try:
    from gdx_dispatch.routers import stripe_webhook
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: stripe_webhook")
    stripe_webhook = APIRouter(tags=["stripe"])

try:
    from gdx_dispatch.routers import stripe_connect as stripe_connect_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: stripe_connect_router")
    stripe_connect_router = APIRouter(tags=["stripe-connect"])

try:
    from gdx_dispatch.routers import audit as audit_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: audit_router")
    audit_router = APIRouter(prefix="/api/audit", tags=["audit"])

try:
    from gdx_dispatch.routers import communications as communications_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: communications_router")
    communications_router = APIRouter(tags=["communications"])

try:
    from gdx_dispatch.routers import payments as payments_gdx_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: payments_gdx_router")
    payments_gdx_router = APIRouter(prefix="/payments", tags=["payments"])

try:
    from gdx_dispatch.routers import expenses as expenses_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: expenses_router")
    expenses_router = APIRouter(prefix="/api", tags=["expenses"])

try:
    from gdx_dispatch.routers import customers as customers_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: customers_router")
    customers_router = APIRouter(prefix="/api/customers", tags=["customers"])

try:
    from gdx_dispatch.routers import segments as segments_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: segments_router")
    segments_router = APIRouter(prefix="/api/segments", tags=["segments"])

try:
    from gdx_dispatch.routers import invoices as invoices_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: invoices_router")
    invoices_router = APIRouter(prefix="/api/invoices", tags=["invoices"])

try:
    from gdx_dispatch.routers import documents as documents_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: documents_router")
    documents_router = APIRouter(tags=["documents"])

try:
    from gdx_dispatch.routers import uploads as uploads_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: uploads_router")
    uploads_router = APIRouter(tags=["uploads"])

try:
    from gdx_dispatch.routers import pdf as pdf_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: pdf_router")
    pdf_router = APIRouter(tags=["pdf"])

try:
    from gdx_dispatch.routers import mobile as mobile_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: mobile_router")
    mobile_router = APIRouter(prefix="/api/mobile", tags=["mobile"])

try:
    from gdx_dispatch.routers import mobile_quoting as mobile_quoting_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: mobile_quoting_router")
    mobile_quoting_router = APIRouter(prefix="/api/mobile", tags=["mobile-quoting"])

try:
    from gdx_dispatch.routers import mobile_invoicing as mobile_invoicing_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: mobile_invoicing_router")
    mobile_invoicing_router = APIRouter(prefix="/api/mobile", tags=["mobile-invoicing"])

try:
    from gdx_dispatch.routers import mobile_day_summary as mobile_day_summary_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: mobile_day_summary_router")
    mobile_day_summary_router = APIRouter(prefix="/api/mobile", tags=["mobile-day-summary"])

try:
    from gdx_dispatch.routers import mobile_chat as mobile_chat_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: mobile_chat_router")
    mobile_chat_router = APIRouter(prefix="/api/mobile", tags=["mobile-chat"])

try:
    from gdx_dispatch.routers import reports as reports_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: reports_router")
    reports_router = APIRouter(prefix="/api/reports", tags=["reports"])

try:
    from gdx_dispatch.routers import labor as labor_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: labor_router")
    labor_router = APIRouter(prefix="/api", tags=["labor"])

try:
    from gdx_dispatch.routers import tech_efficiency as tech_efficiency_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: tech_efficiency_router")
    tech_efficiency_router = APIRouter(prefix="/api/reports/tech-efficiency", tags=["reports"])

try:
    from gdx_dispatch.routers import budgets as budgets_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: budgets_router")
    budgets_router = APIRouter(prefix="/api/budgets", tags=["budgets"])

try:
    from gdx_dispatch.routers import automations as automations_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: automations_router")
    automations_router = APIRouter(prefix="/api/automations", tags=["automations"])

try:
    from gdx_dispatch.routers import warranties as warranties_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: warranties_router")
    warranties_router = APIRouter(prefix="/api/warranties", tags=["warranties"])

try:
    from gdx_dispatch.routers import catalog as catalog_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: catalog_router")
    catalog_router = APIRouter(tags=["catalog"])

try:
    from gdx_dispatch.routers import inventory as inventory_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: inventory_router")
    inventory_router = APIRouter(tags=["inventory"])

try:
    from gdx_dispatch.routers import vendors as vendors_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: vendors_router")
    vendors_router = APIRouter(tags=["vendors"])

try:
    from gdx_dispatch.routers import purchase_orders as purchase_orders_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: purchase_orders_router")
    purchase_orders_router = APIRouter(tags=["purchase_orders"])

try:
    from gdx_dispatch.routers import change_orders as change_orders_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: change_orders_router")
    change_orders_router = APIRouter(tags=["change_orders"])

try:
    from gdx_dispatch.routers import maintenance as maintenance_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("maintenance_router_load_failed")
    maintenance_router = APIRouter(tags=["maintenance"])

try:
    from gdx_dispatch.routers import gdpr as gdpr_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("gdpr_router_load_failed")
    gdpr_router = APIRouter(tags=["gdpr"])

try:
    from gdx_dispatch.routers import collections as collections_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: collections_router")
    collections_router = APIRouter(tags=["collections"])

try:
    from gdx_dispatch.routers import invoice_reminders as invoice_reminders_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("invoice_reminders_router_load_failed")
    invoice_reminders_router = APIRouter(tags=["invoice_reminders"])

try:
    from gdx_dispatch.routers import tasks as tasks_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("tasks_router_load_failed")
    tasks_router = APIRouter(tags=["tasks"])

try:
    from gdx_dispatch.routers import proposals as proposals_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("proposals_router_load_failed")
    proposals_router = APIRouter(tags=["proposals"])

try:
    from gdx_dispatch.routers import appointments as appointments_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("appointments_router_load_failed")
    appointments_router = APIRouter(tags=["appointments"])

try:
    from gdx_dispatch.routers import gps as gps_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("gps_router_load_failed")
    gps_router = APIRouter(tags=["gps"])

try:
    from gdx_dispatch.routers import leads as leads_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("leads_router_load_failed")
    leads_router = APIRouter(tags=["leads"])

try:
    from gdx_dispatch.routers import scheduling as scheduling_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("scheduling_router_load_failed")
    scheduling_router = APIRouter(tags=["scheduling"])

try:
    from gdx_dispatch.routers import payroll as payroll_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("payroll_router_load_failed")
    payroll_router = APIRouter(tags=["payroll"])

try:
    from gdx_dispatch.routers import exports as exports_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("exports_router_load_failed")
    exports_router = APIRouter(tags=["exports"])

try:
    from gdx_dispatch.routers import job_costing as job_costing_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("job_costing_router_load_failed")
    job_costing_router = APIRouter(tags=["job_costing"])

try:
    from gdx_dispatch.routers import onboarding as onboarding_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("onboarding_router_load_failed")
    onboarding_router = APIRouter(tags=["onboarding"])

try:
    from gdx_dispatch.routers import tours as tours_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("tours_router_load_failed")
    tours_router = APIRouter(tags=["tours"])

try:
    from gdx_dispatch.routers import ux_telemetry as ux_telemetry_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("ux_telemetry_router_load_failed")
    ux_telemetry_router = APIRouter(tags=["ux_telemetry"])

try:
    from gdx_dispatch.routers import service_agreements as service_agreements_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("service_agreements_router_load_failed")
    service_agreements_router = APIRouter(tags=["service_agreements"])

try:
    from gdx_dispatch.routers import winback as winback_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("winback_router_load_failed")
    winback_router = APIRouter(tags=["winback"])

try:
    from gdx_dispatch.routers import notes as notes_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("notes_router_load_failed")
    notes_router = APIRouter(tags=["notes"])

try:
    from gdx_dispatch.routers import messages as messages_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("messages_router_load_failed")
    messages_router = APIRouter(tags=["team_messages"])

try:
    from gdx_dispatch.routers import signatures as signatures_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("signatures_router_load_failed")
    signatures_router = None

try:
    from gdx_dispatch.routers import inbound_comms as inbound_comms_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("inbound_comms_router_load_failed")
    inbound_comms_router = None

try:
    from gdx_dispatch.routers import surveys as surveys_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("surveys_router_load_failed")
    surveys_router = None

try:
    from gdx_dispatch.routers import photos as photos_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("photos_router_load_failed")
    photos_router = APIRouter(tags=["photos"])

try:
    from gdx_dispatch.routers import tags as tags_router_module
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("tags_router_load_failed")
    tags_router_module = APIRouter(tags=["tags_router"])

try:
    from gdx_dispatch.routers import feature_flags as feature_flags_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("feature_flags_router_load_failed")
    feature_flags_router = APIRouter(tags=["feature_flags"])

try:
    from gdx_dispatch.routers import games as games_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("games_router_load_failed")
    games_router = APIRouter(tags=["games"])

try:
    from gdx_dispatch.routers import activity as activity_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: activity_router")
    activity_router = APIRouter(tags=["activity"])

try:
    from gdx_dispatch.routers import webhooks as webhooks_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("webhooks_router_load_failed")
    webhooks_router = APIRouter(tags=["webhooks"])

try:
    from gdx_dispatch.routers import role_permissions as role_permissions_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("role_permissions_router_load_failed")
    role_permissions_router = APIRouter(tags=["role_permissions"])

try:
    from gdx_dispatch.routers import pricing as pricing_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: pricing_router")
    pricing_router = APIRouter(tags=["pricing"])

try:
    from gdx_dispatch.routers import loyalty as loyalty_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: loyalty_router")
    loyalty_router = APIRouter(prefix="/api/loyalty", tags=["loyalty"])

try:
    from gdx_dispatch.routers import marketing as marketing_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: marketing_router")
    marketing_router = APIRouter(prefix="/api", tags=["marketing"])

try:
    from gdx_dispatch.routers import campaigns as campaigns_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: campaigns_router")
    campaigns_router = APIRouter(tags=["campaigns"])

try:
    from gdx_dispatch.routers import branding_public as branding_public_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: branding_public_router")
    branding_public_router = APIRouter(prefix="/api/settings", tags=["settings-public"])

try:
    from gdx_dispatch.routers import settings as settings_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: settings_router")
    settings_router = APIRouter(prefix="/api/settings", tags=["settings"])

try:
    from gdx_dispatch.routers import admin_settings as admin_settings_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: admin_settings_router")
    admin_settings_router = APIRouter(prefix="/api/admin", tags=["admin-settings"])

try:
    from gdx_dispatch.routers import maps as maps_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: maps_router")
    maps_router = APIRouter(prefix="/api/maps", tags=["maps"])

try:
    from gdx_dispatch.routers import notifications as notifications_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: notifications_router")
    notifications_router = APIRouter(tags=["notifications"])

try:
    from gdx_dispatch.routers import equipment_tracking as equipment_tracking_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: equipment_tracking_router")
    equipment_tracking_router = APIRouter(tags=["equipment-tracking"])

try:
    from gdx_dispatch.routers import timeclock as timeclock_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: timeclock_router")
    timeclock_router = APIRouter(prefix="/api/timeclock", tags=["timeclock-router"])

try:
    from gdx_dispatch.routers import checklists as checklists_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: checklists_router")
    checklists_router = APIRouter(tags=["checklists"])

try:
    from gdx_dispatch.routers import booking as booking_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: booking_router")
    booking_router = APIRouter(tags=["booking"])

try:
    from gdx_dispatch.routers import fleet as fleet_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: fleet_router")
    fleet_router = APIRouter(tags=["fleet-router"])

try:
    from gdx_dispatch.routers import recurring_jobs as recurring_jobs_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: recurring_jobs_router")
    recurring_jobs_router = APIRouter(prefix="/api/recurring", tags=["recurring"])

try:
    from gdx_dispatch.routers import ui_compat as ui_compat_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ui_compat_router")
    ui_compat_router = APIRouter(tags=["ui-compat"])

try:
    from gdx_dispatch.routers import sub_resources as sub_resources_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: sub_resources_router")
    sub_resources_router = APIRouter(tags=["sub-resources"])

try:
    from gdx_dispatch.routers import job_templates as job_templates_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: job_templates_router")
    job_templates_router = APIRouter(prefix="/api/job-templates", tags=["job-templates"])

try:
    from gdx_dispatch.routers import reviews as reviews_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: reviews_router")
    reviews_router = APIRouter(prefix="/api/reviews", tags=["reviews"])

try:
    from gdx_dispatch.routers import referrals as referrals_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: referrals_router")
    referrals_router = APIRouter(prefix="/api/referrals", tags=["referrals"])

try:
    from gdx_dispatch.routers import search as search_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: search_router")
    search_router = APIRouter(prefix="/api/search", tags=["search"])

try:
    from gdx_dispatch.routers import users as users_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: users_router")
    users_router = APIRouter(prefix="/api/users", tags=["users"])

try:
    # Forecasting module — /api/forecast/* + /api/quickbooks/recurring-transactions.
    from gdx_dispatch.modules.forecasting import router as forecasting_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: forecasting_router")
    forecasting_router = None  # type: ignore

try:
    from gdx_dispatch.modules.quickbooks import qb_router as quickbooks
except Exception:
    # 2026-05-20: legacy `gdx_dispatch/routers/quickbooks.py` file DELETED. S122-10
    # already removed the fallback registration here; the file itself was
    # dead code that wrote tokens to a now-orphan `qb_connections` table
    # (caused the 2026-05-20 QB-reconnect-not-visible incident). If the
    # modules-based router fails to import, fail loud.
    logging.getLogger("gdx_dispatch.app").exception("quickbooks module import failed — failing loud")
    quickbooks = APIRouter(tags=["quickbooks"])

try:
    from gdx_dispatch.modules.inventory import router as inventory
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: inventory")
    inventory = APIRouter(tags=["inventory"])

try:
    from gdx_dispatch.modules.equipment import router as equipment
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: equipment")
    equipment = APIRouter(tags=["equipment"])

try:
    from gdx_dispatch.modules.timeclock import router as timeclock
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: timeclock")
    timeclock = APIRouter(tags=["timeclock"])

try:
    from gdx_dispatch.modules.workflows import router as workflows
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: workflows")
    workflows = APIRouter(tags=["workflows"])

try:
    from gdx_dispatch.modules.campaigns import router as campaigns
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: campaigns")
    campaigns = APIRouter(tags=["campaigns"])

try:
    from gdx_dispatch.modules.proposals import router as proposals
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: proposals")
    proposals = APIRouter(tags=["proposals"])

try:
    from gdx_dispatch.modules.fleet import router as fleet
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: fleet")
    fleet = APIRouter(tags=["fleet"])

try:
    from gdx_dispatch.modules.gps_dispatch import router as gps_dispatch
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: gps_dispatch")
    gps_dispatch = APIRouter(tags=["gps_dispatch"])

try:
    from gdx_dispatch.routers import portal as customer_portal_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: customer_portal_router")
    customer_portal_router = APIRouter(tags=["customer_portal"])

try:
    from gdx_dispatch.routers import custom_fields as custom_fields_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("custom_fields_router_load_failed")
    custom_fields_router = APIRouter(tags=["custom_fields"])

try:
    from gdx_dispatch.core.webhooks import monitor as webhook_monitor
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: webhook_monitor")
    webhook_monitor = APIRouter(tags=["webhook_monitor"])

try:
    from gdx_dispatch.core.pwa import PWARouter as pwa_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: pwa_router")
    pwa_router = APIRouter(tags=["pwa"])

try:
    from gdx_dispatch.core.health_score import router as health_score_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: health_score_router")
    health_score_router = APIRouter(tags=["health-scores"])

try:
    from gdx_dispatch.core.distributor_dashboard import distributor_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: distributor_router")
    distributor_router = APIRouter(prefix="/dashboard", tags=["distributor"])

try:
    from gdx_dispatch.modules.distributor.order_portal import dealer_router as dealer_order_router
    from gdx_dispatch.modules.distributor.order_portal import distributor_router as distributor_order_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: dealer_order_router")
    dealer_order_router = APIRouter(prefix="/api/dealer", tags=["dealer-orders"])
    distributor_order_router = APIRouter(prefix="/api/distributor", tags=["distributor-orders"])

try:
    from gdx_dispatch.core.wholesaler_dashboard import wholesaler_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: wholesaler_router")
    wholesaler_router = APIRouter(prefix="/dashboard", tags=["wholesaler"])

try:
    from gdx_dispatch.core.jwks import JWKSRouter as jwks_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: jwks_router")
    jwks_router = APIRouter(tags=["jwks"])

try:
    from gdx_dispatch.core.onboarding import router as core_onboarding_router
    from gdx_dispatch.core.onboarding import ui_router as onboarding_ui_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: core_onboarding_router")
    core_onboarding_router = APIRouter(tags=["onboarding"])
    onboarding_ui_router = APIRouter(tags=["onboarding-ui"])

try:
    from gdx_dispatch.routers.admin_ops import router as admin_ops_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: admin_ops_router")
    admin_ops_router = APIRouter(prefix="/api/admin", tags=["admin"])

try:
    from gdx_dispatch.core.admin_flags import router as admin_flags_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: admin_flags_router")
    admin_flags_router = APIRouter(tags=["feature-flags"])

try:
    from gdx_dispatch.core.feature_flags_router import router as feature_flags_ui_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: feature_flags_ui_router")
    feature_flags_ui_router = APIRouter(tags=["feature-flags-ui"])

try:
    from gdx_dispatch.core.admin_modules import router as admin_modules_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: admin_modules_router")
    admin_modules_router = APIRouter(tags=["tenant-modules"])


try:
    from gdx_dispatch.core.integrations import router as integrations_router
    from gdx_dispatch.core.integrations import ui_router as integrations_ui_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: integrations_router")
    integrations_router = APIRouter(prefix="/api/integrations", tags=["integrations"])
    integrations_ui_router = APIRouter(tags=["integrations-ui"])

try:
    from gdx_dispatch.core.sla_monitor import router as sla_monitor_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: sla_monitor_router")
    sla_monitor_router = APIRouter(tags=["sla"])

try:
    from gdx_dispatch.core.task_monitor import router as task_monitor_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: task_monitor_router")
    task_monitor_router = APIRouter(tags=["task-monitor"])

try:
    from gdx_dispatch.core.push_notifications import router as push_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: push_router")
    push_router = APIRouter(prefix="/api/push", tags=["push"])

try:
    from gdx_dispatch.core.payments import public_router as payments_public_router
    from gdx_dispatch.core.payments import router as payments_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: payments_router")
    payments_router = APIRouter(prefix="/api/payments", tags=["payments"])
    payments_public_router = APIRouter(tags=["payments-public"])

try:
    from gdx_dispatch.core.api_keys import APIKeyMiddleware
    from gdx_dispatch.core.api_keys import router as api_keys_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: api_keys_router")
    api_keys_router = APIRouter(prefix="/api/developer", tags=["developer"])
    APIKeyMiddleware = None  # type: ignore[assignment,misc]

try:
    from gdx_dispatch.core.public_api import router as public_api_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: public_api_router")
    public_api_router = APIRouter(prefix="/v1", tags=["Public API v1"])

try:
    from gdx_dispatch.core.developer_portal import router as developer_portal_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: developer_portal_router")
    developer_portal_router = APIRouter(tags=["developer"])

try:
    from gdx_dispatch.routers.resources import router as resources_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: resources_router")
    resources_router = APIRouter(prefix="/api/resources", tags=["resources"])

try:
    from gdx_dispatch.core.locations import router as locations_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: locations_router")
    locations_router = APIRouter(prefix="/api/locations", tags=["locations"])

try:
    from gdx_dispatch.api.public_router import router as public_v1_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: public_v1_router")
    public_v1_router = APIRouter(prefix="/api/v1", tags=["public-api"])

try:
    from gdx_dispatch.core.tenant_ui import router as tenant_ui_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: tenant_ui_router")
    tenant_ui_router = APIRouter(tags=["tenant-ui"])

try:
    from gdx_dispatch.routers.ai_communication import router as ai_comms_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ai_comms_router")
    ai_comms_router = APIRouter(prefix="/api/ai/communication", tags=["ai-communication"])

try:
    from gdx_dispatch.routers.ai_estimates import router as ai_estimates_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ai_estimates_router")
    ai_estimates_router = APIRouter(prefix="/api/ai/estimates", tags=["ai-estimates"])

try:
    from gdx_dispatch.routers.supplier_portal import router as supplier_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: supplier_router")
    supplier_router = APIRouter(prefix="/api/supplier", tags=["supplier-portal"])

try:
    from gdx_dispatch.routers.door_catalog import router as door_catalog_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: door_catalog_router")
    door_catalog_router = APIRouter(prefix="/api/catalog", tags=["door-catalog"])

try:
    from gdx_dispatch.routers.install_sheet import router as install_sheet_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: install_sheet_router")
    install_sheet_router = APIRouter(tags=["install-sheet"])

try:
    from gdx_dispatch.routers.planner import router as planner_router_mod
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: planner_router_mod")
    planner_router_mod = APIRouter(prefix="/api/planner", tags=["planner"])

try:
    from gdx_dispatch.routers.supplier_invite import router as supplier_invite_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: supplier_invite_router")
    supplier_invite_router = APIRouter(tags=["supplier-portal"])

try:
    from gdx_dispatch.core.audit_dashboard import router as audit_dashboard_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: audit_dashboard_router")
    audit_dashboard_router = APIRouter(tags=["audit-dashboard"])

try:
    from gdx_dispatch.core.platform_analytics import analytics_page_router
    from gdx_dispatch.core.platform_analytics import router as platform_analytics_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: platform_analytics_router")
    platform_analytics_router = APIRouter(prefix="/api/platform", tags=["platform-analytics"])
    analytics_page_router = APIRouter(tags=["analytics-ui"])

try:
    from gdx_dispatch.core.ai_recommendations import router as ai_recommendations_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ai_recommendations_router")
    ai_recommendations_router = APIRouter(prefix="/api", tags=["recommendations"])


try:
    from gdx_dispatch.core.recommendation_routes import router as recommendation_routes_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: recommendation_routes_router")
    recommendation_routes_router = APIRouter(prefix="/api", tags=["recommendations"])

try:
    from gdx_dispatch.core.ai_quote import router as ai_quote_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ai_quote_router")
    ai_quote_router = APIRouter(prefix="/api/ai", tags=["ai-quote"])

try:
    from gdx_dispatch.core.ai_router import router as ai_router_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ai_router_router")
    ai_router_router = APIRouter(prefix="/api/ai", tags=["ai-router"])

try:
    from gdx_dispatch.core.ai_usage_logger import router as ai_usage_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ai_usage_router")
    ai_usage_router = APIRouter(prefix="/api/ai", tags=["ai-usage"])

try:
    from gdx_dispatch.core.performance import (
        SlowEndpointMiddleware,
        SlowQueryMiddleware,
    )
    from gdx_dispatch.core.performance import (
        router as performance_router,
    )
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("import/init failed")
    SlowEndpointMiddleware = None  # type: ignore[assignment,misc]
    SlowQueryMiddleware = None  # type: ignore[assignment,misc]
    performance_router = APIRouter(prefix="/api/admin", tags=["performance"])

try:
    from gdx_dispatch.core.security_logger import router as security_log_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: security_log_router")
    security_log_router = APIRouter(prefix="/api/admin", tags=["security"])

try:
    from gdx_dispatch.core.webhook_logger import router as webhook_delivery_log_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: webhook_delivery_log_router")
    webhook_delivery_log_router = APIRouter(prefix="/api/admin/webhooks", tags=["webhooks-log"])

try:
    from gdx_dispatch.core.data_access_logger import GDPRDataAccessMiddleware
    from gdx_dispatch.core.data_access_logger import router as gdpr_access_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("import/init failed")
    GDPRDataAccessMiddleware = None  # type: ignore[assignment,misc]
    gdpr_access_router = APIRouter(prefix="/api/admin/gdpr", tags=["gdpr-access-log"])

try:
    from gdx_dispatch.core.tenant_metrics import router as tenant_metrics_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: tenant_metrics_router")
    tenant_metrics_router = APIRouter(prefix="/api/admin/metrics", tags=["metrics"])

try:
    from gdx_dispatch.core.rate_limiter import TenantRateLimitMiddleware as _TenantRateLimitMiddleware
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("import/init failed")
    _TenantRateLimitMiddleware = None  # type: ignore[assignment,misc]

try:
    from gdx_dispatch.core.audit_middleware import AuditMiddleware
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("import/init failed")
    AuditMiddleware = None  # type: ignore[assignment,misc]

try:
    from gdx_dispatch.modules.contractors import router as contractors
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: contractors")
    contractors = APIRouter(tags=["contractors"])

try:
    from gdx_dispatch.routers.dispatch_ws import router as dispatch_ws_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: dispatch_ws_router")
    dispatch_ws_router = APIRouter(tags=["dispatch-ws"])

try:
    from gdx_dispatch.core.ai_quote import router as ai_quote_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: ai_quote_router")
    ai_quote_router = APIRouter(prefix="/api/ai", tags=["ai-quotes"])

try:
    from gdx_dispatch.core.parts_pricing import router as parts_pricing_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: parts_pricing_router")
    parts_pricing_router = APIRouter(prefix="/api/parts", tags=["parts-pricing"])

try:
    from gdx_dispatch.routers import van_inventory as van_inventory_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: van_inventory_router")
    van_inventory_router = APIRouter(prefix="/api/van-inventory", tags=["van-inventory"])

try:
    from gdx_dispatch.routers import po_workflow as po_workflow_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: po_workflow_router")
    po_workflow_router = APIRouter(prefix="/api/purchase-orders", tags=["po-workflow"])

try:
    from gdx_dispatch.routers import commission as commission_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: commission_router")
    commission_router = APIRouter(prefix="/api/commissions", tags=["commissions"])

try:
    from gdx_dispatch.routers import service_triggers as service_triggers_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: service_triggers_router")
    service_triggers_router = APIRouter(prefix="/api/service-triggers", tags=["service-triggers"])

try:
    from gdx_dispatch.routers import variance_report as variance_report_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: variance_report_router")
    variance_report_router = APIRouter(prefix="/api/variance", tags=["variance"])

try:
    from gdx_dispatch.routers import warranty_claims as warranty_claims_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: warranty_claims_router")
    warranty_claims_router = APIRouter(prefix="/api/warranty-claims", tags=["warranty-claims"])

try:
    from gdx_dispatch.routers import safety_checklist as safety_checklist_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: safety_checklist_router")
    safety_checklist_router = APIRouter(prefix="/api/safety", tags=["safety"])

try:
    from gdx_dispatch.routers import estimate_nurture as estimate_nurture_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: estimate_nurture_router")
    estimate_nurture_router = APIRouter(prefix="/api/estimate-nurture", tags=["estimate-nurture"])

try:
    from gdx_dispatch.routers import performance as user_performance_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: user_performance_router")
    user_performance_router = APIRouter(prefix="/api/performance", tags=["performance"])

try:
    from gdx_dispatch.routers import email_settings as email_settings_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: email_settings_router")
    from fastapi import APIRouter
    email_settings_router = APIRouter(prefix="/api/settings", tags=["email-settings"])

try:
    from gdx_dispatch.routers import parts_needed as parts_needed_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: parts_needed_router")
    from fastapi import APIRouter
    parts_needed_router = APIRouter(prefix="/api", tags=["parts-needed"])

try:
    from gdx_dispatch.routers import job_assignments as job_assignments_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: job_assignments_router")
    from fastapi import APIRouter
    job_assignments_router = APIRouter(prefix="/api", tags=["job-assignments"])

try:
    from gdx_dispatch.routers import push as push_v2_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: push_v2_router")
    from fastapi import APIRouter
    push_v2_router = APIRouter(prefix="/api/push/v2", tags=["push-v2"])

try:
    from gdx_dispatch.routers import holding_areas as holding_areas_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: holding_areas_router")
    from fastapi import APIRouter
    holding_areas_router = APIRouter(prefix="/api/holding-areas", tags=["holding-areas"])

try:
    from gdx_dispatch.routers import service_calls as service_calls_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: service_calls_router")
    from fastapi import APIRouter
    service_calls_router = APIRouter(prefix="/api/service-calls", tags=["service-calls"])

try:
    from gdx_dispatch.routers import bug_reports as bug_reports_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: bug_reports_router")
    from fastapi import APIRouter
    bug_reports_router = APIRouter(prefix="/api/feedback", tags=["feedback"])

try:
    from gdx_dispatch.routers import support as support_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: support_router")
    from fastapi import APIRouter
    support_router = APIRouter(prefix="/api/support", tags=["support"])

try:
    from gdx_dispatch.routers import instant_estimate as instant_estimate_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: instant_estimate_router")
    instant_estimate_router = APIRouter(prefix="/api/ai", tags=["ai"])

try:
    from gdx_dispatch.routers import pdf_templates as pdf_templates_router
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("Failed to import router: pdf_templates_router")
    pdf_templates_router = APIRouter(prefix="/api/pdf-templates", tags=["pdf-templates"])

# circuit_breaker module-level instances are imported here so they are
# initialised at startup; routes can import them directly from gdx_dispatch.core.circuit_breaker.
try:
    from gdx_dispatch.core.circuit_breaker import (  # noqa: F401 – side-effect import
        email_circuit,
        qb_circuit,
        stripe_circuit,
    )
except Exception:
    logging.getLogger("gdx_dispatch.app").exception("import/init failed")
    qb_circuit = stripe_circuit = email_circuit = None  # type: ignore[assignment]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # CSP covers the legitimate third-party resources the app actually uses:
        # - CloudFlare Insights beacon auto-injected on HTML responses by CF
        # - Sentry SDK loads a blob: Web Worker for Session Replay
        # - Sentry telemetry goes to regional ingest endpoints (e.g. o<id>.ingest.us.sentry.io,
        #   which is a subdomain of sentry.io, NOT of ingest.sentry.io — the old wildcard
        #   pattern missed this because CSP host matching follows DNS suffix rules)
        response.headers["Content-Security-Policy-Report-Only"] = (
            "default-src 'self'; "
            "script-src 'self' https://static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "worker-src 'self' blob:; "
            "connect-src 'self' https://*.sentry.io https://static.cloudflareinsights.com https://cloudflareinsights.com"
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


def _tier_limit(*args: Any, **kwargs: Any) -> str:
    request = kwargs.get("request")
    if request is None and args and isinstance(args[0], Request):
        request = args[0]
    # Bypass rate limiting for E2E test traffic
    if request and os.environ.get("GDX_E2E_BYPASS") == "1" and request.headers.get("x-e2e-test") == "true":
        return "100000/minute"
    tier = (request.headers.get("x-tenant-tier", "Starter") if request else "Starter").strip().lower()
    return "600/minute" if tier == "professional" else "120/minute"


limiter = Limiter(key_func=get_remote_address, default_limits=[_tier_limit])


def _check_customer_facing_config() -> None:
    """Fail loud at startup when env vars that drive customer-facing signals
    are missing in prod. The cost of a missing welcome email or missing DNS
    is a customer who can't reach their account (Becky 2026-04-30: lost a
    full day to PLATFORM_SMTP_PASS being unset). Logs a structured warning
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
    required_for_signup = [
        ("PLATFORM_SMTP_PASS", "welcome emails will fail (Becky 2026-04-30 incident)"),
        ("CLOUDFLARE_API_TOKEN", "tenant subdomains will not be DNS-resolvable"),
        ("CLOUDFLARE_ZONE_ID", "tenant subdomains will not be DNS-resolvable"),
    ]
    missing = [(name, why) for name, why in required_for_signup if not os.environ.get(name)]
    if missing:
        for name, why in missing:
            log.error("STARTUP_CONFIG_MISSING var=%s impact=%s env=%s", name, why, env)
        # Don't refuse to start — admin/dashboard/MCP traffic still works
        # without these. But the error is now plainly visible in container
        # logs and Sentry, instead of buried in a per-request warning that
        # only fires when a real customer is trying to sign up.


def _check_encryption_at_rest() -> None:
    """S122-1 (T1): refuse to boot in production when MASTER_ENCRYPTION_KEY
    is unset. ``gdx_dispatch.core.pii._FERNET`` and ``gdx_dispatch.core.database._FERNET``
    fall back to ``None`` when the key is missing, which makes the QB OAuth
    token-store helpers and ``tenants.db_url_enc`` round-trip plaintext.
    Columns named ``*_enc`` would then hold cleartext credentials. The
    fallback is intentional for dev/test (``test_01_gdx_scaffold.py:303``
    pins the contract); the gate fires only when GDX_ENV is
    production-ish.

    Live consumers covered by this gate (post S122-1c, every
    ``EncryptedString`` model has been retyped to plain ``Text``):
      * ``qb_token_store.access_token_enc`` / ``refresh_token_enc``
        (manual ``_encrypt`` helpers, ``gdx_dispatch.modules.quickbooks.oauth``)
      * ``tenants.db_url_enc`` (``gdx_dispatch.core.database._decrypt_db_url``)

    NOT protected here: ``Customer.{name,email,phone,address}``,
    ``webhook_endpoints.secret``, ``integration_configs.secret``. Those
    are plain ``Text`` post-S122-1c — typing matches the actual stored
    plaintext bytes. See ``ai-queue/plans/sprint_encryption_rollout_proper.md``
    for the Option C path if encryption returns for any of them.
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
                "REFUSING TO BOOT: pii.encryption_status() scan failed: %s. "
                "An EncryptedString column may exist on an unimported base. "
                "Fix the import error or override with GDX_ENV=dev."
                % status.scan_error
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("SENTRY_DSN", "")
    env = os.environ.get("GDX_ENV", "development")
    observability.init_sentry(dsn=dsn, env=env)
    observability.init_otel(service_name="gdx-api", app=app)
    _check_encryption_at_rest()
    _check_customer_facing_config()
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
    app = FastAPI(
        title="GDX API",
        description="DispatchApp — Multi-tenant field service dispatch platform API. "
                    "Manages jobs, customers, estimates, invoices, technician dispatch, "
                    "and AI-powered quoting for garage door service companies.",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
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
    if SlowQueryMiddleware is not None:
        app.add_middleware(SlowQueryMiddleware)
    if SlowEndpointMiddleware is not None:
        app.add_middleware(SlowEndpointMiddleware)
    if GDPRDataAccessMiddleware is not None:
        app.add_middleware(GDPRDataAccessMiddleware)
    if AuditMiddleware is not None:
        app.add_middleware(AuditMiddleware)
    if APIKeyMiddleware is not None:
        app.add_middleware(APIKeyMiddleware)
    try:
        from gdx_dispatch.core.service_accounts import ServiceKeyMiddleware
        app.add_middleware(ServiceKeyMiddleware)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("service_key_middleware_unavailable")
    if _TenantRateLimitMiddleware is not None:
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
    # Target request flow (outermost → innermost):
    #     TenantMiddleware → SPIFFEAuthMiddleware → TenantRoleMiddleware
    #     → APIVersioningMiddleware → ConsumerAuditMiddleware
    #     → CrossTenantAccessMiddleware → IdempotencyMiddleware → handler
    # Written below in reverse (innermost first) so the LAST line (Tenant)
    # ends up outermost.
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

    # SS-32 SPIFFE workload attestation (opt-in via SPIFFE_ENABLE env).
    try:
        if os.getenv("SPIFFE_ENABLE", "").lower() in ("1", "true", "yes"):
            from gdx_dispatch.core.middleware.spiffe_auth_middleware import SPIFFEAuthMiddleware
            from gdx_dispatch.core.spiffe.spire_trust_bundle import TrustBundleCache as _SpiffeTrustBundle

            _spiffe_audiences = [
                a.strip()
                for a in os.getenv("SPIFFE_EXPECTED_AUDIENCES", "gdx-api").split(",")
                if a.strip()
            ]
            app.add_middleware(
                SPIFFEAuthMiddleware,
                trust_bundle=_SpiffeTrustBundle(),
                expected_audiences=_spiffe_audiences,
            )
        else:
            logging.getLogger("gdx_dispatch.app").info("ss32_spiffe_middleware_disabled: SPIFFE_ENABLE not set")
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("ss32_spiffe_middleware_unavailable")

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
                eng = create_engine(url, connect_args={"connect_timeout": 2})
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

        # ── Probe control-db via CONTROL_DATABASE_URL (same as app startup) ─
        control_url = os.environ.get(
            "CONTROL_DATABASE_URL",
            os.environ.get("DATABASE_URL", ""),
        )
        if control_url and not _probe(control_url):
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
    app.include_router(
        stripe_connect_router.router if hasattr(stripe_connect_router, "router") else stripe_connect_router
    )
    app.include_router(audit_router.router if hasattr(audit_router, "router") else audit_router)
    app.include_router(
        communications_router.router if hasattr(communications_router, "router") else communications_router
    )
    _comms_public = getattr(communications_router, "public_router", None)
    if _comms_public is not None:
        app.include_router(_comms_public)
    app.include_router(payments_gdx_router.router if hasattr(payments_gdx_router, "router") else payments_gdx_router)
    app.include_router(expenses_router.router if hasattr(expenses_router, "router") else expenses_router)
    app.include_router(customers_router.router if hasattr(customers_router, "router") else customers_router)
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
    app.include_router(automations_router.router if hasattr(automations_router, "router") else automations_router)
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
    app.include_router(tasks_router.router if hasattr(tasks_router, "router") else tasks_router)
    app.include_router(proposals_router.router if hasattr(proposals_router, "router") else proposals_router)
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
    if signatures_router is not None:
        # Public routes first so `/api/signatures/token/{token}` matches before the
        # admin `/api/signatures/{document_type}/{document_id}` collision path.
        app.include_router(signatures_router.public_router)
        app.include_router(signatures_router.admin_router)
    if inbound_comms_router is not None:
        app.include_router(inbound_comms_router.public_router)
        app.include_router(inbound_comms_router.admin_router)
    if surveys_router is not None:
        app.include_router(surveys_router.public_router)
        app.include_router(surveys_router.admin_router)
    app.include_router(photos_router.router if hasattr(photos_router, "router") else photos_router)
    app.include_router(tags_router_module.router if hasattr(tags_router_module, "router") else tags_router_module)
    app.include_router(feature_flags_router.router if hasattr(feature_flags_router, "router") else feature_flags_router)
    app.include_router(games_router.router if hasattr(games_router, "router") else games_router)
    app.include_router(activity_router.router if hasattr(activity_router, "router") else activity_router)
    app.include_router(webhooks_router.router if hasattr(webhooks_router, "router") else webhooks_router)
    app.include_router(role_permissions_router.router if hasattr(role_permissions_router, "router") else role_permissions_router)
    app.include_router(pricing_router.router if hasattr(pricing_router, "router") else pricing_router)
    # Sprint 1.0.5 — pricing-engine admin endpoints (tier sets + volume discount + preview)
    try:
        from gdx_dispatch.routers import pricing_admin as _pricing_admin_router
        app.include_router(_pricing_admin_router.router)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("Failed to import router: pricing_admin")
    # Sprint S97 — labor pricing matrix admin (size/SKU-keyed flat-rate labor)
    try:
        from gdx_dispatch.routers import labor_pricing_admin as _labor_pricing_admin_router
        app.include_router(_labor_pricing_admin_router.router)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("Failed to import router: labor_pricing_admin")
    # Sprint S97 slice 8 — labor variance (estimated vs actual hours/cost)
    try:
        from gdx_dispatch.routers import labor_variance as _labor_variance_router
        app.include_router(_labor_variance_router.router)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("Failed to import router: labor_variance")
    # Sprint vendor-statement-recon — Midwest statement upload + parse
    try:
        from gdx_dispatch.routers import vendor_statements as _vendor_statements_router
        app.include_router(_vendor_statements_router.router)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("Failed to import router: vendor_statements")
    app.include_router(loyalty_router.router if hasattr(loyalty_router, "router") else loyalty_router)
    app.include_router(marketing_router.router if hasattr(marketing_router, "router") else marketing_router)
    app.include_router(campaigns_router.router if hasattr(campaigns_router, "router") else campaigns_router)
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
    # equipment_tracking_router (legacy EquipmentAsset surface) was unwired
    # 2026-05-03 in favor of the canonical CustomerEquipment surface in
    # gdx_dispatch/modules/equipment/router.py. Both registered the same /api/equipment
    # paths; legacy was winning by registration order and writing to the
    # parallel `equipment_assets` table. See ai-queue/brainstorm/
    # gap_equipment_router_consolidation.md for the audit + backfill plan.
    # The model + table are kept for backward read access via the prune step
    # in gdx_dispatch/tools/migrate_equipment_consolidation.py.
    app.include_router(timeclock_router.router if hasattr(timeclock_router, "router") else timeclock_router)
    app.include_router(checklists_router.router if hasattr(checklists_router, "router") else checklists_router)
    app.include_router(booking_router.router if hasattr(booking_router, "router") else booking_router)
    app.include_router(fleet_router.router if hasattr(fleet_router, "router") else fleet_router)
    app.include_router(
        recurring_jobs_router.router if hasattr(recurring_jobs_router, "router") else recurring_jobs_router
    )
    # Sub-resource endpoints (customer recurring-jobs, job line-items, proposals,
    # billing, AI quality) — real DB-backed implementations replacing shims.
    app.include_router(sub_resources_router.router if hasattr(sub_resources_router, "router") else sub_resources_router)
    # UI compat shim — thin handlers for Vue view endpoints that don't yet
    # have a dedicated router implementation. Returns empty lists / default
    # shapes so the UI renders without errors. MUST be registered AFTER all
    # real routers so that any real endpoint wins on path conflicts.
    app.include_router(ui_compat_router.router if hasattr(ui_compat_router, "router") else ui_compat_router)
    app.include_router(
        job_templates_router.router if hasattr(job_templates_router, "router") else job_templates_router
    )
    app.include_router(reviews_router.router if hasattr(reviews_router, "router") else reviews_router)
    app.include_router(referrals_router.router if hasattr(referrals_router, "router") else referrals_router)
    app.include_router(search_router.router if hasattr(search_router, "router") else search_router)
    app.include_router(users_router.router if hasattr(users_router, "router") else users_router)
    app.include_router(quickbooks.router if hasattr(quickbooks, "router") else quickbooks)
    if forecasting_router is not None:
        app.include_router(forecasting_router.router)
    # NOTE: gdx_dispatch/modules/*/router.py (legacy) and gdx_dispatch/routers/*.py (newer) both
    # register some overlapping paths with the same function names. The newer
    # versions are richer and tenant-scoped; the legacy modules have some
    # unique endpoints (e.g. /inventory/parts/{id}/stock, /timeclock/report,
    # /jobs/{id}/parts). We keep BOTH mounted so no path coverage is lost.
    # FastAPI routes by first-match order (newer routers are registered
    # first, above); the legacy modules only serve paths the new ones don't.
    # This does produce "Duplicate Operation ID" warnings during openapi()
    # generation — those are noise, tracked for follow-up merge.
    app.include_router(inventory.router if hasattr(inventory, "router") else inventory)
    app.include_router(equipment.router if hasattr(equipment, "router") else equipment)
    app.include_router(timeclock.router if hasattr(timeclock, "router") else timeclock)
    app.include_router(workflows.router if hasattr(workflows, "router") else workflows)
    app.include_router(campaigns.router if hasattr(campaigns, "router") else campaigns)
    app.include_router(proposals.router if hasattr(proposals, "router") else proposals)
    app.include_router(fleet.router if hasattr(fleet, "router") else fleet)
    app.include_router(gps_dispatch.router if hasattr(gps_dispatch, "router") else gps_dispatch)
    app.include_router(
        customer_portal_router.router if hasattr(customer_portal_router, "router") else customer_portal_router
    )
    app.include_router(custom_fields_router.router if hasattr(custom_fields_router, "router") else custom_fields_router)
    app.include_router(webhook_monitor.router if hasattr(webhook_monitor, "router") else webhook_monitor)
    app.include_router(pwa_router if hasattr(pwa_router, "routes") else APIRouter())
    app.include_router(health_score_router.router if hasattr(health_score_router, "router") else health_score_router)
    app.include_router(distributor_router, prefix="/api/distributor", tags=["distributor"])
    app.include_router(wholesaler_router, prefix="/api/wholesaler", tags=["wholesaler"])
    app.include_router(jwks_router)
    app.include_router(core_onboarding_router, prefix="/api", tags=["onboarding"])
    # onboarding_ui_router (Flask-era HTML wizard at /onboarding + /onboarding/{step})
    # is no longer mounted — the Vue SPA's /onboarding route owns those paths now.
    # The router object is still importable for gdx_dispatch/tests/test_24_onboarding.py.
    app.include_router(admin_ops_router)
    app.include_router(admin_flags_router, prefix="/api/admin", tags=["feature-flags"])
    app.include_router(feature_flags_ui_router)
    app.include_router(admin_modules_router, prefix="/api/admin", tags=["tenant-modules"])
    app.include_router(push_router)
    app.include_router(locations_router)
    app.include_router(tenant_ui_router, prefix="/legacy", tags=["tenant-ui"])
    app.include_router(ai_comms_router)
    app.include_router(ai_estimates_router)
    app.include_router(supplier_router)
    app.include_router(supplier_invite_router)
    app.include_router(door_catalog_router)
    app.include_router(planner_router_mod)
    app.include_router(audit_dashboard_router)
    app.include_router(platform_analytics_router)
    app.include_router(analytics_page_router)
    app.include_router(ai_recommendations_router)
    app.include_router(recommendation_routes_router)
    app.include_router(ai_quote_router)
    app.include_router(ai_router_router)
    app.include_router(ai_usage_router)
    app.include_router(performance_router)
    app.include_router(security_log_router)
    app.include_router(webhook_delivery_log_router)
    app.include_router(gdpr_access_router)
    app.include_router(tenant_metrics_router)
    app.include_router(api_keys_router)
    app.include_router(public_api_router)
    app.include_router(developer_portal_router)
    app.include_router(payments_router)
    app.include_router(payments_public_router)
    app.include_router(distributor_order_router)
    app.include_router(dealer_order_router)
    app.include_router(task_monitor_router)
    app.include_router(prometheus_router)

    try:
        from gdx_dispatch.routers.auth import sso as sso_router
        app.include_router(sso_router.router if hasattr(sso_router, "router") else sso_router)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("Failed to import router: sso_router")

    try:
        from gdx_dispatch.routers import dispatch_scheduling as dispatch_sched_router
        app.include_router(dispatch_sched_router.router if hasattr(dispatch_sched_router, "router") else dispatch_sched_router)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("Failed to import router: dispatch_scheduling")

    try:
        from gdx_dispatch.routers import voice as voice_router
        app.include_router(voice_router.router if hasattr(voice_router, "router") else voice_router)
    except Exception:
        logging.getLogger("gdx_dispatch.app").exception("Failed to import router: voice")

    # Task monitoring dashboard page — serves task_monitor.html at /admin/tasks
    from pathlib import Path as _Path

    from fastapi import Depends as _Depends
    from fastapi.responses import HTMLResponse as _HTMLResponse

    _tmpl = _Path(__file__).parent / "templates" / "task_monitor.html"

    @app.get("/admin/tasks", response_class=_HTMLResponse, include_in_schema=False)
    async def admin_tasks_page() -> _HTMLResponse:
        if _tmpl.exists():
            return _HTMLResponse(content=_tmpl.read_text())
        return _HTMLResponse(content="<h1>Task monitor template not found</h1>", status_code=200)

    # Superadmin control panel — serves superadmin.html at /superadmin
    _sa_tmpl = _Path(__file__).parent / "templates" / "superadmin.html"

    @app.get("/superadmin", response_class=_HTMLResponse, include_in_schema=False)
    async def superadmin_dashboard_page() -> _HTMLResponse:
        """Superadmin control panel — requires ADMIN_API_TOKEN via JS."""
        if _sa_tmpl.exists():
            return _HTMLResponse(content=_sa_tmpl.read_text())
        return _HTMLResponse(content="<h1>Superadmin template not found</h1>", status_code=200)

    from typing import Annotated as _Annotated

    from gdx_dispatch.core import status_page as _status_page
    from gdx_dispatch.core.modules import require_role as _require_role
    app.include_router(_status_page.router)
    app.include_router(_status_page.admin_router)

    _SlaAdminDep = _Annotated[None, _Depends(_require_role("admin", "owner"))]

    @app.get("/admin/sla-report", response_class=_HTMLResponse, include_in_schema=False)
    async def admin_sla_report(_: _SlaAdminDep) -> _HTMLResponse:
        """Admin SLA report — renders the public status page with auth guard."""
        _tmpl_sla = _Path(__file__).parent / "templates" / "status_page.html"
        if _tmpl_sla.exists():
            return _HTMLResponse(content=_tmpl_sla.read_text())
        return _HTMLResponse(content="<h1>SLA report template not found</h1>", status_code=200)

    @app.get("/api/status", tags=["sla"], include_in_schema=True)
    async def api_status() -> dict:
        """Public status endpoint — returns current platform status JSON."""
        return _status_page.get_current_status()

    @app.get("/status", response_class=_HTMLResponse, include_in_schema=False)
    async def status_html() -> _HTMLResponse:
        """Public status page — no authentication required."""
        _tmpl_status = _Path(__file__).parent / "templates" / "status_page.html"
        if _tmpl_status.exists():
            return _HTMLResponse(content=_tmpl_status.read_text())
        return _HTMLResponse(content="<h1>Status page not found</h1>", status_code=200)

    app.include_router(integrations_router)
    app.include_router(integrations_ui_router)
    app.include_router(sla_monitor_router)
    app.include_router(contractors.router if hasattr(contractors, "router") else contractors)
    app.include_router(van_inventory_router.router if hasattr(van_inventory_router, "router") else van_inventory_router)
    app.include_router(po_workflow_router.router if hasattr(po_workflow_router, "router") else po_workflow_router)
    app.include_router(commission_router.router if hasattr(commission_router, "router") else commission_router)
    app.include_router(service_triggers_router.router if hasattr(service_triggers_router, "router") else service_triggers_router)
    app.include_router(variance_report_router.router if hasattr(variance_report_router, "router") else variance_report_router)
    app.include_router(warranty_claims_router.router if hasattr(warranty_claims_router, "router") else warranty_claims_router)
    app.include_router(safety_checklist_router.router if hasattr(safety_checklist_router, "router") else safety_checklist_router)
    app.include_router(estimate_nurture_router.router if hasattr(estimate_nurture_router, "router") else estimate_nurture_router)
    app.include_router(user_performance_router.router if hasattr(user_performance_router, "router") else user_performance_router)
    app.include_router(email_settings_router.router if hasattr(email_settings_router, "router") else email_settings_router)
    app.include_router(parts_needed_router.router if hasattr(parts_needed_router, "router") else parts_needed_router)
    app.include_router(job_assignments_router.router if hasattr(job_assignments_router, "router") else job_assignments_router)
    app.include_router(push_v2_router.router if hasattr(push_v2_router, "router") else push_v2_router)
    app.include_router(holding_areas_router.router if hasattr(holding_areas_router, "router") else holding_areas_router)
    app.include_router(service_calls_router.router if hasattr(service_calls_router, "router") else service_calls_router)
    app.include_router(bug_reports_router.router if hasattr(bug_reports_router, "router") else bug_reports_router)
    app.include_router(support_router.router if hasattr(support_router, "router") else support_router)
    app.include_router(instant_estimate_router.router if hasattr(instant_estimate_router, "router") else instant_estimate_router)
    app.include_router(pdf_templates_router.router if hasattr(pdf_templates_router, "router") else pdf_templates_router)
    app.include_router(dispatch_ws_router)
    # ai_quote_router is already included at line ~1125 — don't register twice
    # (was causing Duplicate Operation ID warnings for api_quote_history/feedback)
    app.include_router(parts_pricing_router)
    app.include_router(resources_router)
    app.include_router(public_v1_router)

    # Sprint 1.x-S26: admin AI settings router.
    try:
        from gdx_dispatch.routers import admin_ai_settings as ai_settings_mod
        app.include_router(ai_settings_mod.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire admin_ai_settings (1.x-S26)')

    # Sprint tech_mobile S1-Z4: per-tenant tech-mobile feature settings.
    try:
        from gdx_dispatch.routers import admin_tech_mobile_settings as tech_mobile_settings_mod
        app.include_router(tech_mobile_settings_mod.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire admin_tech_mobile_settings (S1-Z4)')

    # Sprint tech_mobile S1-A8: customer-alert tag taxonomy CRUD.
    try:
        from gdx_dispatch.routers import admin_customer_tags as customer_tags_mod
        app.include_router(customer_tags_mod.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire admin_customer_tags (S1-A8)')

    # Sprint phone-com pc-s8/s9/s10/s11/s12: integration card + ops + webhooks.
    try:
        from gdx_dispatch.routers import phone_com_settings as pc_settings_mod
        app.include_router(pc_settings_mod.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire phone_com_settings router (pc-s8)')
    try:
        from gdx_dispatch.modules.phone_com import router as pc_ops_router
        app.include_router(pc_ops_router.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire phone_com ops router (pc-s9/s10/s11)')
    try:
        from gdx_dispatch.modules.phone_com import webhook_router as pc_webhook_router
        app.include_router(pc_webhook_router.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire phone_com webhook router (pc-s12)')

    # Sprint Outlook Integration: OAuth + read views + send + webhook receiver.
    try:
        from gdx_dispatch.routers import outlook_oauth as outlook_oauth_mod
        app.include_router(outlook_oauth_mod.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire outlook_oauth router')
    try:
        from gdx_dispatch.modules.outlook import views_router as outlook_views_router
        app.include_router(outlook_views_router.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire outlook views_router')
    try:
        from gdx_dispatch.modules.outlook import send_router as outlook_send_router
        app.include_router(outlook_send_router.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire outlook send_router')
    try:
        from gdx_dispatch.modules.outlook import webhook_router as outlook_webhook_router
        app.include_router(outlook_webhook_router.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire outlook webhook_router')
    try:
        from gdx_dispatch.modules.outlook import admin_settings_router as outlook_admin_router
        app.include_router(outlook_admin_router.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire outlook admin_settings_router')
    try:
        from gdx_dispatch.modules.outlook import folders_router as outlook_folders_router
        app.include_router(outlook_folders_router.router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire outlook folders_router')

    # 2026-04-29 — Tax module (default rate per tenant + customer exemptions).
    # Sprint-shaped so jurisdiction lookup, category overrides, and
    # provider plugins (Avalara/TaxJar) can layer in without a refactor.
    try:
        from gdx_dispatch.modules.tax.router import router as tax_router
        app.include_router(tax_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire tax module')

    # 2026-04-29 / UX audit F-11 — Numbering module (per-tenant job number
    # format + counter). Same module shape as tax: today only jobs use it,
    # tomorrow estimates/invoices share the same format engine.
    try:
        from gdx_dispatch.modules.numbering.router import router as numbering_router
        app.include_router(numbering_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire numbering module')

    # 2026-04-29 / UX audit F-8 — Job workflow flags (per-tenant toggles
    # for schedule lock, arrival event, arrival SMS, complete-time
    # required fields). Default behavior baked into routers/jobs.py.
    try:
        from gdx_dispatch.modules.workflow.router import router as workflow_router
        app.include_router(workflow_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire workflow module')

    # 2026-04-29 / UX audit F-18 — Self-hosted error sink (replaces Sentry).
    # Captures 5xx + unhandled exceptions to control-plane server_errors.
    try:
        from gdx_dispatch.modules.error_sink.router import router as error_sink_router
        app.include_router(error_sink_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire error_sink module')

    # 2026-04-29 / UX audit F-36 — billing terms (per-tenant payment-terms
    # defaults + per-class overrides + early-pay / late-fee / interest config).
    try:
        from gdx_dispatch.modules.billing_terms.router import router as billing_terms_router
        app.include_router(billing_terms_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire billing_terms module')

    # 2026-04-29 / UX audit F-74 — Catalog description policy.
    try:
        from gdx_dispatch.modules.catalog_policy.router import router as catalog_policy_router
        app.include_router(catalog_policy_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire catalog_policy module')

    # 2026-04-30 — Estimates feature toggles (per-line margin override, etc.).
    try:
        from gdx_dispatch.modules.estimates_features.router import router as estimates_features_router
        app.include_router(estimates_features_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire estimates_features module')

    # 2026-05-01 — Dispatch settings (scheduled-no-tech gates + lane visibility).
    try:
        from gdx_dispatch.modules.dispatch_settings.router import router as dispatch_settings_router
        app.include_router(dispatch_settings_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire dispatch_settings module')

    # 2026-04-29 / UX audit F-82 — Payroll module (true vs estimated cost).
    # Local var name MUST NOT be `payroll_router` — that name binds to the
    # module-level import at app.py:274, and a function-local assignment
    # would shadow it for the whole create_app scope, causing
    # UnboundLocalError at the earlier app.include_router(payroll_router) site.
    try:
        from gdx_dispatch.modules.payroll.router import router as f82_payroll_router
        app.include_router(f82_payroll_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire payroll module')

    # 2026-04-29 / UX audit F-89 — Maps provider selector.
    # Same shadow-trap as the F-82 payroll case — `maps_router` is
    # already a function-scope name from an earlier include_router
    # block. Use a distinct name.
    try:
        from gdx_dispatch.modules.maps_provider.router import router as f89_maps_provider_router
        app.include_router(f89_maps_provider_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception('failed to wire maps_provider module')

    # Sprint 1.x-S14: per-tenant AI assistant skeleton (`/api/ai/ask`).
    # Defensive try/except matches the SS router pattern below — a broken
    # AI dependency must not take down the whole app.
    try:
        from gdx_dispatch.routers import ai as ai_router
        app.include_router(ai_router.router if hasattr(ai_router, "router") else ai_router)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("failed to wire ai router (sprint 1.x-S14)")

    # -----------------------------------------------------------------
    # Sprint 0.9-n: SS-14..35 platform routers.
    #
    # Each SS router is wired via try/except so a single broken import
    # doesn't take down the whole app (matches the legacy defensive
    # pattern used above for other routers).
    # -----------------------------------------------------------------
    _ss_log = logging.getLogger("gdx_dispatch.app")
    # Slice 8 (auth-cluster consolidation): the auth-cluster modules
    # moved into gdx_dispatch.routers.auth.*; non-auth SS routers stay flat under
    # gdx_dispatch.routers.*. Dotted import paths are explicit so a future move
    # of any of these gets caught at edit-time, not lost in the silent
    # except branch.
    # Command Center / SaaS-platform routers were removed for this single-tenant
    # release (their tables are gone from the squashed baseline). Only the
    # app-level metadata endpoints. (The PAT/SCIM identity cluster was
    # removed with the single-tenant cleanup.)
    _ss_routers: list[tuple[str, str, str]] = [
        # (SS-label, dotted-import-path, friendly-name-for-logs)
        ("SS-25", "gdx_dispatch.routers.api_metadata", "api_metadata"),
        ("SS-26", "gdx_dispatch.routers.well_known", "well_known"),
    ]
    for _ss_label, _ss_dotted, _ss_friendly in _ss_routers:
        try:
            _ss_mod = __import__(_ss_dotted, fromlist=["router"])
            _ss_router = getattr(_ss_mod, "router", _ss_mod)
            app.include_router(_ss_router)
        except Exception:
            _ss_log.exception("ss_router_unavailable: %s/%s", _ss_label, _ss_friendly)

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
