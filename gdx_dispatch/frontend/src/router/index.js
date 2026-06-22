import { createRouter, createWebHistory } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import { getLoginRedirectLocation } from '../lib/auth-urls';
import { useViewMode } from '../composables/useViewMode';

// Critical views — loaded eagerly
import LoginView from '../views/LoginView.vue';
import DashboardView from '../views/DashboardView.vue';
import JobsView from '../views/JobsView.vue';
import CustomersView from '../views/CustomersView.vue';
import DispatchView from '../views/DispatchView.vue';

// Non-critical views — lazy loaded for smaller initial bundle
const EstimatesView = () => import('../views/EstimatesView.vue');
const EstimateView = () => import('../views/EstimateView.vue');
const BillingView = () => import('../views/BillingView.vue');
const PaymentsView = () => import('../views/PaymentsView.vue');
const InvoiceDetailView = () => import('../views/InvoiceDetailView.vue');
const InvoiceCreateView = () => import('../views/InvoiceCreateView.vue');
const SettingsView = () => import('../views/SettingsView.vue');
const UserProfileView = () => import('../views/UserProfileView.vue');
const InvoiceRemindersView = () => import('../views/InvoiceRemindersView.vue');
const JobDetailView = () => import('../views/JobDetailView.vue');
const CustomerDetailView = () => import('../views/CustomerDetailView.vue');
const InventoryView = () => import('../views/InventoryView.vue');
const TimeclockView = () => import('../views/TimeclockView.vue');
const DailyLoadsheetView = () => import('../views/DailyLoadsheetView.vue');
const DeliveryLoadsheetView = () => import('../views/DeliveryLoadsheetView.vue');
const PlannerView = () => import('../views/PlannerView.vue');
const EquipmentView = () => import('../views/EquipmentView.vue');
const CommunicationsView = () => import('../views/CommunicationsView.vue');
const SchedulingView = () => import('../views/SchedulingView.vue');
const CampaignsView = () => import('../views/CampaignsView.vue');
const AutomationsView = () => import('../views/AutomationsView.vue');
const WinbackView = () => import('../views/WinbackView.vue');
const ExpensesView = () => import('../views/ExpensesView.vue');
const ForecastingView = () => import('../views/ForecastingView.vue');
const RecurringStreamsView = () => import('../views/RecurringStreamsView.vue');
const MonthlyBudgetView = () => import('../views/MonthlyBudgetView.vue');
const SpendingTrendsView = () => import('../views/SpendingTrendsView.vue');
const LoyaltyView = () => import('../views/LoyaltyView.vue');
const ReportsView = () => import('../views/ReportsView.vue');
const DocumentsView = () => import('../views/DocumentsView.vue');
const FleetView = () => import('../views/FleetView.vue');
const MobileTodayView = () => import('../views/MobileTodayView.vue');
const MobileJobsView = () => import('../views/MobileJobsView.vue');
const MobileSummaryView = () => import('../views/MobileSummaryView.vue');
const MobileDispatchView = () => import('../views/MobileDispatchView.vue');
const MobilePlannerView = () => import('../views/MobilePlannerView.vue');
const MobileCustomersView = () => import('../views/MobileCustomersView.vue');
const MobileCustomerDetailView = () => import('../views/MobileCustomerDetailView.vue');
const MobileTimeclockView = () => import('../views/MobileTimeclockView.vue');
const MobileInboxView = () => import('../views/MobileInboxView.vue');
const MobileEstimatesView = () => import('../views/MobileEstimatesView.vue');
const MobileBillingView = () => import('../views/MobileBillingView.vue');
const MobileInventoryView = () => import('../views/MobileInventoryView.vue');
const MobilePartsToOrderView = () => import('../views/MobilePartsToOrderView.vue');
const TechMobileSettingsView = () => import('../views/admin/TechMobileSettingsView.vue');
const CatalogView = () => import('../views/CatalogView.vue');
const VendorsView = () => import('../views/VendorsView.vue');
const PurchaseOrdersView = () => import('../views/PurchaseOrdersView.vue');
const ChangeOrdersView = () => import('../views/ChangeOrdersView.vue');
const ReferralsView = () => import('../views/ReferralsView.vue');
const AdminOpsView = () => import('../views/AdminOpsView.vue');
const DatabaseAdminView = () => import('../views/DatabaseAdminView.vue');
const NotFoundView = () => import('../views/NotFoundView.vue');
const AIAssistantView = () => import('../views/AIAssistantView.vue');
const PhoneComCallsView = () => import('../views/PhoneComCallsView.vue');
const PhoneComMessagesView = () => import('../views/PhoneComMessagesView.vue');
const PhoneComColdLeadsView = () => import('../views/PhoneComColdLeadsView.vue');
const PhoneComFaxesView = () => import('../views/PhoneComFaxesView.vue');
const OutlookSettingsView = () => import('../views/admin/OutlookSettingsView.vue');
const InboxView = () => import('../views/InboxView.vue');
const EquipmentTrackingView = () => import('../views/EquipmentTrackingView.vue');
const JobTemplatesView = () => import('../views/JobTemplatesView.vue');
const PayrollView = () => import('../views/PayrollView.vue');
const OnboardingView = () => import('../views/OnboardingView.vue');
const ServiceAgreementsView = () => import('../views/ServiceAgreementsView.vue');
const MaintenanceView = () => import('../views/MaintenanceView.vue');
const AppointmentsView = () => import('../views/AppointmentsView.vue');
const BookingView = () => import('../views/BookingView.vue');
const LeadsView = () => import('../views/LeadsView.vue');
const ProposalsView = () => import('../views/ProposalsView.vue');
const SignaturesView = () => import('../views/SignaturesView.vue');
const TechniciansView = () => import('../views/TechniciansView.vue');
const JobCostingView = () => import('../views/JobCostingView.vue');
const TagsView = () => import('../views/TagsView.vue');
const PhotosView = () => import('../views/PhotosView.vue');
const WebhooksView = () => import('../views/WebhooksView.vue');
const GdprView = () => import('../views/GdprView.vue');
const FeatureFlagsView = () => import('../views/FeatureFlagsView.vue');
const GameCatalogView = () => import('../views/GameCatalogView.vue');
const GamePlayerView = () => import('../views/GamePlayerView.vue');
const RolePermissionsView = () => import('../views/RolePermissionsView.vue');
const UsersView = () => import('../views/UsersView.vue');
const TasksView = () => import('../views/TasksView.vue');
const CustomFieldsView = () => import('../views/CustomFieldsView.vue');
const SurveysView = () => import('../views/SurveysView.vue');
const ActivityView = () => import('../views/ActivityView.vue');
const ChecklistsView = () => import('../views/ChecklistsView.vue');
const GpsView = () => import('../views/GpsView.vue');
const ExportsView = () => import('../views/ExportsView.vue');
// Sprint 1.2: SS-29..35 platform-admin views
// SS-29 ShadowMigrations, SS-30 CutoverControl, SS-32 SpiffeWorkloads,
// SS-34 DrDrills, SS-35 PrivacyRequests moved to Command Center 2026-05-03 (S92).
// ResourceTypes view removed 2026-05-05 — SS-33 backend (`/api/resource-types`) is unmounted; view 404'd on every load.
// 2026-04-29 / UX audit F-82 — payroll module admin view (entries / config).
const AdminPayrollView = () => import('../views/admin/PayrollView.vue');
const CollectionsView = () => import('../views/CollectionsView.vue');
const SegmentsView = () => import('../views/SegmentsView.vue');
const ReviewsView = () => import('../views/ReviewsView.vue');
const WarrantiesView = () => import('../views/WarrantiesView.vue');
const MapsView = () => import('../views/MapsView.vue');
const ResourcesView = () => import('../views/ResourcesView.vue');
const SsoView = () => import('../views/SsoView.vue');
const PortalView = () => import('../views/PortalView.vue');
const CustomerPortalView = () => import('../views/CustomerPortalView.vue');
const PricingView = () => import('../views/PricingView.vue');
const MarginTiersView = () => import('../views/MarginTiersView.vue');
const LaborMatrixView = () => import('../views/LaborMatrixView.vue');
const VendorStatementsView = () => import('../views/VendorStatementsView.vue');
const VendorStatementDetailView = () => import('../views/VendorStatementDetailView.vue');
const QuickbooksView = () => import('../views/QuickbooksView.vue');
const PdfTemplateEditorView = () => import('../views/PdfTemplateEditorView.vue');
const PartsToOrderView = () => import('../views/PartsToOrderView.vue');
const CommissionsView = () => import('../views/CommissionsView.vue');
const VarianceReportView = () => import('../views/VarianceReportView.vue');
const PerformanceView = () => import('../views/PerformanceView.vue');

// Sprint 0.9-o: SS-14..35 admin/user routes
// 2026-05-05 audit pruned views whose backends never mounted (SS-27/SS-33/custom-field-sensitivity/SAR-erasure).
const AuditLogViewer = () => import('../views/AuditLogViewer.vue');


const routes = [
  { path: '/', redirect: '/dashboard' },
  // `noShell: true` keeps these routes out of the AppLayout shell mounted
  // by App.vue. Login/signup/portal pages render their own full-screen
  // shells; the not-found fallback is bare; onboarding is a full-screen
  // wizard. Every other route is wrapped by App.vue's AppLayout so the
  // sidebar/topbar/bottom-nav are stable across navigations.
  { path: '/login', name: 'login', component: LoginView, meta: { public: true, noShell: true } },
  { path: '/forgot-password', name: 'forgot-password', component: () => import('../views/ForgotPasswordView.vue'), meta: { public: true, noShell: true } },
  { path: '/reset-password', name: 'reset-password', component: () => import('../views/ResetPasswordView.vue'), meta: { public: true, noShell: true } },
  { path: '/signup', name: 'signup', component: () => import('../views/SignupView.vue'), meta: { public: true, noShell: true } },
  { path: '/onboarding', name: 'onboarding', component: () => import('../views/OnboardingView.vue'), meta: { noShell: true } },
  { path: '/customer-portal', name: 'customer-portal', component: CustomerPortalView, meta: { public: true, noSidebar: true, noShell: true } },
  { path: '/dashboard', name: 'dashboard', component: DashboardView },
  { path: '/jobs', name: 'jobs', component: JobsView },
  { path: '/jobs/:id', name: 'job-detail', component: JobDetailView },
  { path: '/customers', name: 'customers', component: CustomersView },
  { path: '/customers/duplicates', name: 'customer-duplicates', component: () => import('../views/CustomerDuplicatesView.vue') },
  { path: '/portal', name: 'portal', component: PortalView },
  { path: '/referrals', name: 'referrals', component: ReferralsView },
  { path: '/customers/:id', name: 'customer-detail', component: CustomerDetailView },
  { path: '/dispatch', name: 'dispatch', component: DispatchView },
  { path: '/scheduling', name: 'scheduling', component: SchedulingView },
  { path: '/gps', name: 'gps', component: GpsView },
  { path: '/maps', name: 'maps', component: MapsView },
  { path: '/tasks', name: 'tasks', component: TasksView },
  // /messages → /communications (deduped 2026-04-29 per modules.js cleanup;
  // route kept as redirect so existing bookmarks resolve).
  { path: '/messages', redirect: '/communications' },
  { path: '/reviews', name: 'reviews', component: ReviewsView },
  { path: '/estimates', name: 'estimates', component: EstimatesView },
  { path: '/estimates/new', name: 'estimate-create', component: EstimateView },
  { path: '/estimates/:id', name: 'estimate-detail', component: EstimateView },
  { path: '/invoices', redirect: '/billing' },
  { path: '/billing', name: 'billing', component: BillingView, meta: { requiresPermission: 'invoices.read_all' } },
  { path: '/billing/new', name: 'invoice-create', component: InvoiceCreateView, meta: { requiresPermission: 'invoices.write' } },
  { path: '/payments', name: 'payments', component: PaymentsView, meta: { requiresPermission: 'payments.read' } },
  { path: '/expenses', name: 'expenses', component: ExpensesView, meta: { requiresPermission: 'accounting.read' } },
  { path: '/forecasting', name: 'forecasting', component: ForecastingView, meta: { requiresPermission: 'accounting.read' } },
  { path: '/forecasting/recurring', name: 'recurring-streams', component: RecurringStreamsView, meta: { requiresPermission: 'accounting.read' } },
  { path: '/budget', name: 'budget', component: MonthlyBudgetView, meta: { requiresPermission: 'accounting.read' } },
  { path: '/spending-trends', name: 'spending-trends', component: SpendingTrendsView, meta: { requiresPermission: 'accounting.read' } },
  { path: '/collections', name: 'collections', component: CollectionsView, meta: { requiresPermission: 'invoices.read_all' } },
  { path: '/exports', name: 'exports', component: ExportsView },
  { path: '/invoice-reminders', name: 'invoice-reminders', component: InvoiceRemindersView, meta: { requiresPermission: 'invoices.read_all' } },
  { path: '/billing/:id', name: 'invoice-detail', component: InvoiceDetailView },
  { path: '/settings', name: 'settings', component: SettingsView },
  { path: '/profile', name: 'profile', component: UserProfileView },
  { path: '/sso', name: 'sso', component: SsoView },
  // (duplicate /onboarding registration removed 2026-05-09 — already declared
  //  above at the top of the public-route block; Vue Router warned + the
  //  second declaration silently won. Single declaration now.)
  { path: '/inventory', name: 'inventory', component: InventoryView },
  { path: '/timeclock', name: 'timeclock', component: TimeclockView },
  { path: '/equipment', name: 'equipment', component: EquipmentView },
  { path: '/communications', name: 'communications', component: CommunicationsView },
  // /voice → /phone-com (deduped 2026-04-29). Bookmark redirect.
  { path: '/voice', redirect: '/phone-com' },
  { path: '/segments', name: 'segments', component: SegmentsView },
  // /inbound-comms → /communications (deduped 2026-04-29). Bookmark redirect.
  { path: '/inbound-comms', redirect: '/communications' },
  { path: '/surveys', name: 'surveys', component: SurveysView },
  { path: '/campaigns', name: 'campaigns', component: CampaignsView },
  // /marketing → /campaigns (deduped 2026-04-29). Bookmark redirect.
  { path: '/marketing', redirect: '/campaigns' },
  { path: '/loyalty', name: 'loyalty', component: LoyaltyView },
  { path: '/automations', name: 'automations', component: AutomationsView },
  { path: '/winback', name: 'winback', component: WinbackView },
  { path: '/reports', name: 'reports', component: ReportsView },
  { path: '/pricing', name: 'pricing', component: PricingView },
  { path: '/margin-tiers', name: 'margin-tiers', component: MarginTiersView },
  { path: '/labor-matrix', name: 'labor-matrix', component: LaborMatrixView },
  { path: '/vendor-statements', name: 'vendor-statements', component: VendorStatementsView, meta: { requiresPermission: 'vendor_statements.read' } },
  { path: '/vendor-statements/:id', name: 'vendor-statement-detail', component: VendorStatementDetailView, meta: { requiresPermission: 'vendor_statements.read' } },
  { path: '/quickbooks', name: 'quickbooks', component: QuickbooksView },
  { path: '/documents', name: 'documents', component: DocumentsView },
  // /uploads → /documents (merged 2026-04-29). Bookmark redirect.
  { path: '/uploads', redirect: '/documents' },
  { path: '/resources', name: 'resources', component: ResourcesView },
  { path: '/activity', name: 'activity', component: ActivityView },
  { path: '/fleet', name: 'fleet', component: FleetView },
  { path: '/daily-loadsheet', name: 'daily-loadsheet', component: DailyLoadsheetView },
  { path: '/delivery-loadsheet', name: 'delivery-loadsheet', component: DeliveryLoadsheetView },
  { path: '/planner', name: 'planner', component: PlannerView },
  { path: '/catalog', name: 'catalog', component: CatalogView },
  { path: '/vendors', name: 'vendors', component: VendorsView },
  { path: '/purchase-orders', name: 'purchase-orders', component: PurchaseOrdersView },
  { path: '/change-orders', name: 'change-orders', component: ChangeOrdersView },
  { path: '/warranties', name: 'warranties', component: WarrantiesView },
  { path: '/admin-ops', name: 'admin-ops', component: AdminOpsView, meta: { requiresPermission: 'settings.write' } },
  { path: '/admin/database', name: 'admin-database', component: DatabaseAdminView, meta: { requiresPermission: 'settings.write' } },
  // /admin-settings → /settings (deduped). Bookmark redirect.
  { path: '/admin-settings', redirect: '/settings' },
  // /ai-settings was removed when AI Assistant config moved to
  // Settings → Integrations (Sprint 1.x). Keep a redirect so any
  // bookmarked link or stale toast notification lands the user in the
  // right place rather than 404'ing.
  { path: '/ai-settings', redirect: '/settings' },
  { path: '/ai-assistant', name: 'ai-assistant', component: AIAssistantView },
  { path: '/phone-com/calls', name: 'phone-com-calls', component: PhoneComCallsView },
  { path: '/phone-com/messages', name: 'phone-com-messages', component: PhoneComMessagesView },
  { path: '/phone-com/cold-leads', name: 'phone-com-cold-leads', component: PhoneComColdLeadsView },
  { path: '/phone-com/faxes', name: 'phone-com-faxes', component: PhoneComFaxesView },
  { path: '/settings/integrations/outlook', name: 'outlook-settings', component: OutlookSettingsView },
  { path: '/inbox', name: 'inbox', component: InboxView },
  { path: '/equipment-tracking', name: 'equipment-tracking', component: EquipmentTrackingView },
  { path: '/job-templates', name: 'job-templates', component: JobTemplatesView },
  { path: '/payroll', name: 'payroll', component: PayrollView, meta: { requiresPermission: 'payroll.read' } },
  { path: '/checklists', name: 'checklists', component: ChecklistsView },
  { path: '/service-agreements', name: 'service-agreements', component: ServiceAgreementsView },
  { path: '/maintenance', name: 'maintenance', component: MaintenanceView },
  { path: '/appointments', name: 'appointments', component: AppointmentsView },
  { path: '/booking', name: 'booking', component: BookingView },
  { path: '/leads', name: 'leads', component: LeadsView, meta: { requiresPermission: 'leads.read' } },
  { path: '/proposals', name: 'proposals', component: ProposalsView },
  { path: '/signatures', name: 'signatures', component: SignaturesView },
  { path: '/technicians', name: 'technicians', component: TechniciansView },
  { path: '/job-costing', name: 'job-costing', component: JobCostingView },
  { path: '/photos', name: 'photos', component: PhotosView },
  { path: '/tags', name: 'tags', component: TagsView },
  { path: '/webhooks', name: 'webhooks', component: WebhooksView, meta: { requiresPermission: 'webhooks.manage' } },
  { path: '/gdpr', name: 'gdpr', component: GdprView, meta: { requiresPermission: 'settings.write' } },
  { path: '/feature-flags', name: 'feature-flags', component: FeatureFlagsView, meta: { requiresPermission: 'settings.write' } },
  { path: '/admin/games', name: 'GameCatalog', component: GameCatalogView },
  { path: '/admin/games/:slug', name: 'GamePlayer', component: GamePlayerView },
  { path: '/role-permissions', name: 'role-permissions', component: RolePermissionsView, meta: { requiresPermission: 'settings.write' } },
  { path: '/users', name: 'users', component: UsersView, meta: { requiresPermission: 'users.read' } },
  { path: '/custom-fields', name: 'custom-fields', component: CustomFieldsView, meta: { requiresPermission: 'settings.write' } },
  { path: '/pdf-templates', name: 'pdf-templates', component: PdfTemplateEditorView },
  { path: '/parts-to-order', name: 'parts-to-order', component: PartsToOrderView },
  { path: '/commissions', name: 'commissions', component: CommissionsView },
  { path: '/variance-report', name: 'variance-report', component: VarianceReportView },
  { path: '/performance', name: 'performance', component: PerformanceView },
  { path: '/mobile', name: 'mobile', component: MobileTodayView, meta: { noSidebar: true } },
  { path: '/mobile/jobs', name: 'mobile-jobs', component: MobileJobsView, meta: { noSidebar: true } },
  { path: '/mobile/summary', name: 'mobile-summary', component: MobileSummaryView, meta: { noSidebar: true } },
  { path: '/mobile/dispatch', name: 'mobile-dispatch', component: MobileDispatchView, meta: { noSidebar: true, requiresPermission: 'mobile.dispatch_view' } },
  { path: '/mobile/planner', name: 'mobile-planner', component: MobilePlannerView, meta: { noSidebar: true } },
  { path: '/mobile/customers', name: 'mobile-customers', component: MobileCustomersView, meta: { noSidebar: true } },
  { path: '/mobile/customers/:id', name: 'mobile-customer-detail', component: MobileCustomerDetailView, meta: { noSidebar: true } },
  { path: '/mobile/timeclock', name: 'mobile-timeclock', component: MobileTimeclockView, meta: { noSidebar: true } },
  { path: '/mobile/inbox', name: 'mobile-inbox', component: MobileInboxView, meta: { noSidebar: true } },
  { path: '/mobile/estimates', name: 'mobile-estimates', component: MobileEstimatesView, meta: { noSidebar: true } },
  { path: '/mobile/billing', name: 'mobile-billing', component: MobileBillingView, meta: { noSidebar: true, requiresPermission: 'invoices.read_all' } },
  { path: '/mobile/inventory', name: 'mobile-inventory', component: MobileInventoryView, meta: { noSidebar: true } },
  { path: '/mobile/parts-to-order', name: 'mobile-parts-to-order', component: MobilePartsToOrderView, meta: { noSidebar: true } },
  // Sprint 0.9-o: SS-14..35 admin/user routes
  { path: '/admin/audit-log', name: 'admin-audit-log', component: AuditLogViewer },
  // 2026-05-05 — removed routes for platform-admin views whose backends never mounted.
  // 2026-06-22 — removed /admin/billing-usage + /admin/federation (single-tenant cleanup).
  { path: '/admin/payroll', name: 'admin-payroll', component: AdminPayrollView, meta: { requiresPermission: 'payroll.read' } },
  { path: '/admin/feature-settings/tech-mobile', name: 'admin-feature-settings-tech-mobile', component: TechMobileSettingsView, meta: { requiresPermission: 'settings.write' } },
  { path: '/feedback', name: 'feedback', component: () => import('../views/FeedbackPortalView.vue') },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView, meta: { public: true, noShell: true } },
];

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory(),
    routes,
  });

  router.onError((err) => {
    const msg = err && err.message ? err.message : '';
    const isChunkLoadError =
      /Failed to fetch dynamically imported module/.test(msg) ||
      /Importing a module script failed/.test(msg) ||
      /error loading dynamically imported module/.test(msg);
    if (!isChunkLoadError) return;
    // One-shot reload: if we already reloaded for this reason in the last
    // 10s, don't loop — the chunk is genuinely broken, surface the error.
    const key = 'gdx:chunk-reload-at';
    const last = Number(sessionStorage.getItem(key) || 0);
    if (Date.now() - last < 10_000) return;
    sessionStorage.setItem(key, String(Date.now()));
    window.location.reload();
  });

  router.beforeEach((to) => {
    const auth = useAuthStore();

    if (!to.meta.public && !auth.isAuthenticated) {
      return getLoginRedirectLocation(to.fullPath);
    }

    if (to.path === '/login' && auth.isAuthenticated) {
      // OAuth bridge: if claude.ai (or any external connector) sent the
      // browser through /login?redirect=/oauth/... and we're already
      // authenticated, do NOT bounce to /dashboard — that loses the
      // OAuth chain. Instead, full-nav to the redirect target so the
      // server endpoint sees the session cookie and mints the code.
      const redirect = to.query && to.query.redirect ? String(to.query.redirect) : '';
      if (redirect.startsWith('/oauth/')) {
        window.location.assign(redirect);
        return false;
      }
      return { path: '/dashboard' };
    }

    if (auth.isAuthenticated && !to.meta.public && to.path !== '/login') {
      const viewMode = useViewMode();
      // Tech role gets redirected from desktop-shaped jobs/dispatch/planner
      // surfaces to their mobile-shaped equivalents. /jobs has a card-based
      // /mobile/jobs alternative; everything else falls back to /mobile.
      const role = String(auth.user?.role || '').toLowerCase();
      const isTech = role === 'technician' || role === 'tech';
      if (isTech) {
        // Tech role: redirect from desktop-shaped surfaces to mobile.
        // /jobs has a card-based mobile equivalent; everything else
        // falls back to /mobile (today's route is the tech's home).
        // /customers, /invoices, /estimates: tech doesn't have the
        // permissions to read these AND the table layout is unusable
        // on a phone-sized viewport — sending them to /mobile is
        // strictly an improvement.
        if (to.path === '/jobs') return { path: '/mobile/jobs' };
        if (to.path === '/timeclock') return { path: '/mobile/timeclock' };
        if (to.path === '/dispatch' || to.path === '/planner' || to.path === '/mobile/planner') return { path: '/mobile' };
        // DT-2: /dashboard renders admin-shape KPIs (Revenue Billed, Overdue
        // Invoices, audit-logs feed) and fires two 403-firing API calls on
        // mount for tech (audit/logs + jobs/ready-for-billing). Tech's home
        // is /mobile (MobileTodayView — today's jobs + clock state).
        if (
          to.path === '/customers' ||
          to.path === '/invoices' ||
          to.path === '/billing' ||
          to.path === '/estimates' ||
          to.path === '/reports' ||
          to.path === '/admin' ||
          to.path === '/settings' ||
          to.path === '/dashboard'
        ) {
          return { path: '/mobile' };
        }
      }
      if (viewMode.shouldAutoRedirectToMobile(to.path)) {
        return { path: '/mobile' };
      }
      // MH-5: non-tech mobile users land on the desktop /customers table
      // by default and hit the systemic horizontal-overflow finding (audit
      // P1 #3). When a card-stack mobile companion exists for the route,
      // redirect there. Tech-role users are already handled above.
      const companion = viewMode.mobileCompanionFor(to.path);
      if (companion) return { path: companion, query: to.query };
    }

    // Sprint role-permissions 2.3 — permission gate.
    // Backend remains the source of truth (every gated route 403s on its
    // own); this just keeps users out of pages they can't use. Redirect
    // to /dashboard rather than 403 — the backend will enforce if the
    // route is hit directly.
    if (auth.isAuthenticated && to.meta.requiresPermission) {
      if (!auth.permissionsLoaded) {
        // Best-effort prefetch; let the navigation continue. The next
        // navigation will see permissionsLoaded and gate correctly.
        auth.loadPermissions().catch(() => {});
        return true;
      }
      if (!auth.hasPermission(to.meta.requiresPermission)) {
        return { path: '/dashboard' };
      }
    }

    return true;
  });

  return router;
}
