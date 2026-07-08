"""
Central model registry — importing this module registers ALL tenant models
on TenantBase.metadata, ensuring create_all() picks them all up regardless
of import order.
"""
from gdx_dispatch.models.tenant_models import (  # noqa: F401
    Appointment,
    # Core
    AppSettings,
    AutomationEnrollment,
    AutomationSequence,
    AutomationStep,
    # Phase 3: DDL replacement models (all _ensure_tables DDL now has ORM equivalents)
    BookingJob,
    BookingRequest,
    BugReport,
    ChangeOrderLine,
    Checklist,
    ChecklistItem,
    ChecklistTemplate,
    # Phase 2: models for tables that had no ORM model (2026-04-12)
    ChiDoorCatalog,
    ChiPartsCatalog,
    ClientError,
    CommissionEntry,
    CommissionRule,
    Company,
    # Schema conflict resolutions
    CompanyModuleGrant,
    CustomCatalog,
    CustomCatalogItem,
    Customer,
    DoorSpec,
    # New models for blocked tables
    CustomerLocation,
    CustomerReview,
    Document,
    DocumentFolder,
    DocumentSignature,
    EmailSetting,
    EquipmentAsset,
    EquipmentAssetHistory,
    EstimateNurtureLog,
    EstimateNurtureRule,
    Expense,
    ExpenseLine,
    FleetServiceLog,
    FleetVehicle,
    FollowUp,
    HoldingArea,
    InboundEmail,
    InboundSMS,
    InternalTask,
    InventoryItem,
    Invoice,
    InvoiceLine,
    Job,
    # Inline DDL replacements
    JobDependency,
    JobNote,
    JobPartNeeded,
    JobPhoto,
    JobTemplate,
    LandingLead,
    Lead,
    LoyaltyPoints,
    LoyaltyReferral,
    LoyaltyTier,
    MaintenancePlan,
    MarketingCampaign,
    MarkupRule,
    Message,
    MessageThread,
    MessageThreadMember,
    MobileSyncAction,
    Notification,
    NotificationSentHistory,
    NotificationSettings,
    NotificationTemplate,
    OnboardingState,
    OverheadObligation,
    Payment,
    PaymentReminder,
    PdfTemplate,
    Plan,
    PlanEnrollment,
    PlannerTask,
    PlanStep,
    PORequest,
    PORequestLine,
    PortalBookingRequest,
    PortalMessage,
    Proposal,
    RecurringJobSchedule,
    ReminderSettings,
    Resource,
    ReviewRequest,
    RolePermission,
    SafetyChecklist,
    Segment,
    ServiceAgreement,
    ServiceAgreementTemplate,
    ServiceTrigger,
    StockAdjustment,
    SupplierAccount,
    SupplierCatalogItem,
    SupplierInvitation,
    SupplierOrder,
    SupplierOrderLine,
    SupplierTenantLink,
    SurveyResponse,
    SurveySend,
    SurveyTemplate,
    Tag,
    TagAssignment,
    TaxJurisdiction,
    TeamMessage,
    TeamMessageRecipient,
    TechCommissionRate,
    Technician,
    TechUnavailability,
    TenantRole,
    TimeclockBreak,
    TimeclockEntry,
    TimeEntry,
    User,
    UserRoleAssignment,
    VanInventoryItem,
    VanInventoryLog,
    Vendor,
    Warranty,
    WarrantyClaim,
    WinbackCampaign,
    WinbackSend,
)

# Import module models so they register on TenantBase.metadata
try:
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine, ProposalTier  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.campaigns.models import CampaignSend  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.inventory.models import JobPart, Part  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine  # noqa: F401
except ImportError:
    pass

# Phase 2: register models from modules/routers/core that use TenantBase
# but were never imported here (2026-04-12 an earlier session)
try:
    from gdx_dispatch.modules.wholesale.models import CatalogItem, ChannelAnalytic, PricingTier  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.contractors.models import Contractor, ContractorAssignment  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.equipment.models import CustomerEquipment, EquipmentServiceHistory  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.customer_portal.models import CustomerUser  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.distributor.models import DealerOrder, DistributorAnalytic  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.notifications.models import DeviceToken, NotificationLog, NotificationPreference  # noqa: F401
except ImportError:
    pass
try:
    # 2026-04-29 — sales tax module (per-tenant rate + customer exemptions).
    from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption  # noqa: F401
except ImportError:
    pass
try:
    # 2026-05-19 — forecasting module (settings + QB recurring mirror).
    # 2026-05-20 — added observed/manual RecurringStream + hits.
    from gdx_dispatch.modules.forecasting.models import (  # noqa: F401
        ForecastSettings,
        QBRecurringTransaction,
        RecurringStream,
        RecurringStreamHit,
    )
except ImportError:
    pass
try:
    # 2026-05-20 — banking module (QB Deposit + Transfer mirror + sync schedule
    # + unified qb_banking_entries for the 5 other bank-touching entities).
    from gdx_dispatch.modules.quickbooks.banking import (  # noqa: F401
        QBBankingEntry,
        QBDeposit,
        QBSyncSchedule,
        QBTransfer,
    )
except ImportError:
    pass
try:
    # 2026-07-02 — GL ledger (Phase 1 core): chart of accounts + append-only
    # journal. Tables built by create_all; integrity triggers in migration 012.
    from gdx_dispatch.modules.ledger.models import (  # noqa: F401
        GlAccount,
        GlJournalEntry,
        GlJournalLine,
        GlPeriodLock,
    )
except ImportError:
    pass
try:
    from gdx_dispatch.modules.gps_dispatch.models import DispatchRoute  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.fleet.models import Vehicle, VehicleServiceRecord  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.timeclock.models import TimeClock  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.workflows.models import WorkflowRule, WorkflowRun  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.reporting.models import SavedReport  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.quickbooks.webhook_models import QBWebhookEvent  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.modules.phone_com.models import (  # noqa: F401
        PhoneComCall,
        PhoneComExtension,
        PhoneComMessage,
        PhoneComNumber,
        PhoneComStatsDaily,
        PhoneComVoicemail,
    )
except ImportError:
    pass
try:
    from gdx_dispatch.modules.outlook.models import (  # noqa: F401
        OutlookAccount,
        OutlookAttachment,
        OutlookMessage,
        OutlookSettings,
        OutlookSubscription,
    )
except ImportError:
    pass
try:
    from gdx_dispatch.routers.change_orders import ChangeOrder  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.routers.custom_fields import CustomFieldDefinition, CustomFieldValue  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.routers.purchase_orders import PurchaseOrder, PurchaseOrderLine  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.routers.webhooks import WebhookDeliveryLog, WebhookSubscription  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.routers.gps import TechnicianLocation  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.core.integrations import IntegrationConfig  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.core.next_action import NextAction  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.core.parts_pricing import PartPrice  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.core.quickbooks import QBConnection, QBEntityMap, QBVendor  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.core.ai_quote import QuoteTemplate  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.core.api_keys import APIKey  # noqa: F401
except ImportError:
    pass
try:
    from gdx_dispatch.core.locations import ServiceLocation, UserLocation  # noqa: F401
except ImportError:
    pass

from gdx_dispatch.models.tenant_models import (  # noqa: F401
    # Transitions
    BILLING_TRANSITIONS,
    DISPATCH_TRANSITIONS,
    LIFECYCLE_TRANSITIONS,
    validate_job_transition,
)

# Pricing engine — single source of truth for cost→sell margin math.
# Sprint 1.0.5. Tenant-plane (db-per-tenant; no tenant_id columns).
from gdx_dispatch.models.pricing_engine import (  # noqa: F401
    PRICING_CATEGORIES,
    CustomerVolumeDiscountTier,
    MarginTier,
    PricingClassEnum,
    PricingClassSettings,
    PricingSettings,
    PricingTierSet,
    seed_default_pricing,
)

# Labor pricing matrix — Sprint S97. Tenant-configurable size/SKU-keyed
# flat-rate labor with assumed man-hours. Tenant-plane (no tenant_id columns).
from gdx_dispatch.models.labor_pricing import (  # noqa: F401
    LaborPriceItem,
)
