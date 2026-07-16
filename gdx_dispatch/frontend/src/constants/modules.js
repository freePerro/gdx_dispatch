// 2026-04-29 nav-cleanup pass:
// - default-enabled flips to opt-out (see useTenantModules.js)
// - re-shelved misplaced modules: Warranties → Customers, Campaigns → Marketing,
//   inventory items into their own group, AI Assistant out of Admin (it's already
//   the floating top-bar button)
// - removed dead duplicates: /marketing (= /campaigns), /voice (= /phone-com),
//   /messages + /inbound-comms (= /communications), /admin-settings (= /settings)
// - added Roles & Permissions, Custom Fields, Webhooks, Feature Flags, GDPR,
//   PDF Templates, SaaS Billing, Tags into Admin
// - merged Uploads into Documents (Documents page already lists uploads)
//
// 2026-07-07 tabbed-pages pass: modules that are facets of one job carry a
// `cluster` key. The sidebar shows ONE row per cluster (see NAV_CLUSTERS below
// + useTenantModules) and the cluster's routes render inside a shared
// ModuleTabsPage tab bar. Child entries keep their key (enablement grants),
// their `to` (bookmarks/pins/favorites), and their permission gate — only the
// sidebar presentation collapses. `tabLabel` is the short in-page tab caption
// ("Phone.com Calls" reads as "Calls" inside the Phone page).

export const MODULE_CATEGORIES = [
  {
    key: 'operations',
    label: 'Operations',
    icon: 'pi pi-cog',
    modules: [
      // Field tier (ungated — every role, no permission needed): jobs, timeclock.
      { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/jobs', type: 'Jobs' },
      { key: 'dispatch', label: 'Dispatch', icon: 'pi pi-map', to: '/dispatch', type: 'Jobs', permission: 'nav.office' },
      // 2026-07-01 UX audit: "Scheduling" vs "Appointments" were conflated (both
      // touch job assignment). Renamed + given distinct icons + descriptions:
      // Team Scheduling = reassign jobs across techs; Appointment Confirmations =
      // confirm visit dates with customers.
      { key: 'scheduling', label: 'Team Scheduling', icon: 'pi pi-calendar', to: '/scheduling', type: 'Operations', permission: 'nav.office', description: 'Reassign and reschedule jobs across technicians' },
      { key: 'appointments', label: 'Appointment Confirmations', icon: 'pi pi-calendar-clock', to: '/appointments', type: 'Jobs', permission: 'nav.office', description: 'Confirm upcoming visit dates with customers' },
      { key: 'tasks', label: 'Tasks', icon: 'pi pi-list', to: '/tasks', type: 'Operations', permission: 'nav.office' },
      { key: 'planner', label: 'Planner', icon: 'pi pi-calendar-plus', to: '/planner', type: 'Operations', permission: 'nav.office' },
      { key: 'checklists', label: 'Checklists', icon: 'pi pi-check-square', to: '/checklists', type: 'Operations', permission: 'nav.office' },
      { key: 'job_templates', label: 'Job Templates', icon: 'pi pi-th-large', to: '/job-templates', type: 'Operations', permission: 'nav.office' },
      { key: 'maintenance', label: 'Maintenance Plans', icon: 'pi pi-wrench', to: '/maintenance', type: 'Jobs', permission: 'nav.office' },
      { key: 'technicians', label: 'Technicians', icon: 'pi pi-users', to: '/technicians', type: 'Operations', permission: 'nav.office' },
      { key: 'performance', label: 'Performance', icon: 'pi pi-chart-line', to: '/performance', type: 'Operations', permission: 'nav.office' },
      { key: 'timeclock', label: 'Timeclock', icon: 'pi pi-clock', to: '/timeclock', type: 'Operations' },
      { key: 'fleet', label: 'Fleet', icon: 'pi pi-truck', to: '/fleet', type: 'Operations', permission: 'nav.office', cluster: 'fleet_hub', tabLabel: 'Vehicles' },
      { key: 'gps', label: 'GPS', icon: 'pi pi-compass', to: '/gps', type: 'Operations', permission: 'nav.office', cluster: 'fleet_hub', tabLabel: 'Live GPS' },
      { key: 'maps', label: 'Maps', icon: 'pi pi-globe', to: '/maps', type: 'Operations', permission: 'nav.office', cluster: 'fleet_hub', tabLabel: 'Map' },
      { key: 'daily_loadsheet', label: 'Daily Load Sheet', icon: 'pi pi-check-square', to: '/daily-loadsheet', type: 'Operations', permission: 'nav.office', cluster: 'loadsheets_hub', tabLabel: 'Daily' },
      { key: 'delivery_loadsheet', label: 'Delivery Load Sheet', icon: 'pi pi-truck', to: '/delivery-loadsheet', type: 'Operations', permission: 'nav.office', cluster: 'loadsheets_hub', tabLabel: 'Delivery' },
      { key: 'equipment', label: 'Customer Equipment', icon: 'pi pi-cog', to: '/equipment', type: 'Operations', permission: 'nav.office' },
      { key: 'equipment_tracking', label: 'Company Tools', icon: 'pi pi-database', to: '/equipment-tracking', type: 'Operations', permission: 'nav.office' },
      // Office tier: the default /photos feed is /api/photos/recent, a tenant-wide
      // gallery restricted to dispatch/admin (a tech must not see other jobs'
      // customer-premises photos). Field-tier here showed techs a nav item that
      // 403'd + crashed the page on open (prod incident, 2026-07-10).
      { key: 'photos', label: 'Photos', icon: 'pi pi-images', to: '/photos', type: 'Jobs', permission: 'nav.office' },
    ],
  },
  {
    key: 'customers',
    label: 'Customers',
    icon: 'pi pi-users',
    modules: [
      { key: 'customers', label: 'Customers', icon: 'pi pi-users', to: '/customers', type: 'Customers', permission: 'nav.office' },
      { key: 'customer_portal', label: 'Customer Portal', icon: 'pi pi-id-card', to: '/portal', type: 'Customers', permission: 'nav.office' },
      // Field tier (ungated): communications, inbox.
      // 2026-07-01 UX audit: four messaging destinations are genuinely different
      // channels (different backends) but the labels didn't say so — descriptions
      // disambiguate in the sidebar tooltip.
      { key: 'communications', label: 'Communications', icon: 'pi pi-comments', to: '/communications', type: 'Customers', description: 'Built-in SMS & email threads with customers' },
      { key: 'inbox', label: 'Inbox', icon: 'pi pi-inbox', to: '/inbox', type: 'Operations', description: 'Outlook-synced email inbox' },
      { key: 'phone_com_calls', label: 'Phone.com Calls', icon: 'pi pi-phone', to: '/phone-com/calls', type: 'Customers', requires: 'phone_com', permission: 'nav.office', cluster: 'phone_hub', tabLabel: 'Calls', description: 'Call log from the Phone.com line' },
      { key: 'phone_com_messages', label: 'Phone.com SMS', icon: 'pi pi-comment', to: '/phone-com/messages', type: 'Customers', requires: 'phone_com', permission: 'nav.office', cluster: 'phone_hub', tabLabel: 'SMS', description: 'SMS threads on the Phone.com line (separate from built-in Communications)' },
      { key: 'phone_com_cold_leads', label: 'Phone.com Cold Leads', icon: 'pi pi-user-plus', to: '/phone-com/cold-leads', type: 'Customers', requires: 'phone_com', permission: 'nav.office', cluster: 'phone_hub', tabLabel: 'Cold Leads', description: 'Missed/unreturned callers to follow up on' },
      { key: 'phone_com_faxes', label: 'Phone.com Faxes', icon: 'pi pi-file-pdf', to: '/phone-com/faxes', type: 'Customers', requires: 'phone_com', permission: 'nav.office', cluster: 'phone_hub', tabLabel: 'Faxes', description: 'Faxes received on the Phone.com line' },
      { key: 'reviews', label: 'Reviews', icon: 'pi pi-star', to: '/reviews', type: 'Customers', permission: 'nav.office', cluster: 'reputation_hub', tabLabel: 'Reviews' },
      { key: 'referrals', label: 'Referrals', icon: 'pi pi-share-alt', to: '/referrals', type: 'Customers', permission: 'nav.office', cluster: 'reputation_hub', tabLabel: 'Referrals' },
      { key: 'surveys', label: 'Surveys', icon: 'pi pi-comments', to: '/surveys', type: 'Customers', permission: 'nav.office', cluster: 'reputation_hub', tabLabel: 'Surveys' },
      { key: 'booking', label: 'Online Booking', icon: 'pi pi-calendar', to: '/booking', type: 'Operations', permission: 'nav.office' },
      { key: 'warranties', label: 'Warranties', icon: 'pi pi-shield', to: '/warranties', type: 'Jobs', permission: 'nav.office' },
    ],
  },
  {
    key: 'sales',
    label: 'Sales',
    icon: 'pi pi-chart-line',
    modules: [
      { key: 'leads', label: 'Leads', icon: 'pi pi-user-plus', to: '/leads', type: 'Customers', permission: 'nav.office' },
      { key: 'estimates', label: 'Estimates', icon: 'pi pi-file-edit', to: '/estimates', type: 'Jobs', permission: 'nav.office' },
      { key: 'proposals', label: 'Proposals', icon: 'pi pi-file-edit', to: '/proposals', type: 'Jobs', permission: 'nav.office' },
      { key: 'change_orders', label: 'Change Orders', icon: 'pi pi-refresh', to: '/change-orders', type: 'Jobs', permission: 'nav.office' },
      { key: 'service_agreements', label: 'Service Agreements', icon: 'pi pi-shield', to: '/service-agreements', type: 'Customers', permission: 'nav.office' },
      { key: 'signatures', label: 'Signatures', icon: 'pi pi-pencil', to: '/signatures', type: 'Jobs', permission: 'nav.office' },
    ],
  },
  // 2026-07-01 UX audit: "Financials" was one flat 13-item category with no
  // hierarchy (invoicing vs accounting vs payroll unmapped). Split into three.
  // Module keys are unchanged, so tenant enablement grants keep working.
  {
    key: 'invoicing',
    label: 'Invoicing',
    icon: 'pi pi-dollar',
    modules: [
      { key: 'billing', label: 'Billing', icon: 'pi pi-dollar', to: '/billing', type: 'Invoices', permission: 'invoices.read_all', cluster: 'billing_hub', tabLabel: 'Invoices' },
      { key: 'payments', label: 'Payments', icon: 'pi pi-credit-card', to: '/payments', type: 'Invoices', permission: 'payments.read', cluster: 'billing_hub', tabLabel: 'Payments' },
      { key: 'collections', label: 'Collections', icon: 'pi pi-wallet', to: '/collections', type: 'Invoices', permission: 'invoices.read_all', cluster: 'billing_hub', tabLabel: 'Collections' },
      { key: 'invoice_reminders', label: 'Invoice Reminders', icon: 'pi pi-bell', to: '/invoice-reminders', type: 'Invoices', permission: 'invoices.read_all', cluster: 'billing_hub', tabLabel: 'Reminders' },
    ],
  },
  {
    key: 'accounting',
    label: 'Accounting',
    icon: 'pi pi-calculator',
    modules: [
      { key: 'expenses', label: 'Expenses', icon: 'pi pi-wallet', to: '/expenses', type: 'Invoices', permission: 'accounting.read' },
      { key: 'job_costing', label: 'Job Costing', icon: 'pi pi-chart-line', to: '/job-costing', type: 'Invoices', permission: 'nav.admin' },
      { key: 'pricing', label: 'Pricing', icon: 'pi pi-tags', to: '/pricing', type: 'Invoices', permission: 'nav.admin' },
      { key: 'labor_matrix', label: 'Labor Matrix', icon: 'pi pi-wrench', to: '/labor-matrix', type: 'Invoices', permission: 'pricing.labor_matrix.read' },
      { key: 'vendor_statements', label: 'Vendor Statements', icon: 'pi pi-file-import', to: '/vendor-statements', type: 'Invoices', permission: 'vendor_statements.read' },
      { key: 'vendor_bills', label: 'Vendor Bills', icon: 'pi pi-inbox', to: '/vendor-bills', type: 'Invoices', permission: 'vendor_invoices.read' },
      { key: 'exports', label: 'Exports', icon: 'pi pi-download', to: '/exports', type: 'Invoices', permission: 'nav.admin' },
      { key: 'quickbooks', label: 'QuickBooks', icon: 'pi pi-plug', to: '/quickbooks', type: 'Invoices', permission: 'nav.admin' },
      { key: 'accounting_ledger', label: 'Ledger', icon: 'pi pi-book', to: '/accounting-ledger', type: 'Invoices', permission: 'accounting.read', description: 'Trial balance, P&L, balance sheet, and the journal' },
      { key: 'accounting_settings', label: 'Accounting Settings', icon: 'pi pi-sliders-h', to: '/accounting-settings', type: 'Invoices', permission: 'accounting.read', description: 'Chart of accounts, posting maps, and the ledger master switch' },
    ],
  },
  {
    key: 'payroll_comp',
    label: 'Payroll',
    icon: 'pi pi-money-bill',
    modules: [
      { key: 'payroll', label: 'Payroll', icon: 'pi pi-money-bill', to: '/payroll', type: 'Invoices', permission: 'payroll.read' },
      { key: 'commissions', label: 'Commissions', icon: 'pi pi-percentage', to: '/commissions', type: 'Invoices', permission: 'nav.admin' },
    ],
  },
  {
    // Experimental: features still being validated (e.g. Forecasting's accuracy
    // loop — see docs/forecasting-accuracy-roadmap.md). Grouped here so the
    // "still proving itself" status is explicit in the nav rather than implied
    // by per-item gating. Each item keeps its own permission.
    key: 'experimental',
    label: 'Experimental',
    icon: 'pi pi-sparkles',
    modules: [
      { key: 'reports', label: 'Reports', icon: 'pi pi-chart-bar', to: '/reports', type: 'Invoices', permission: 'nav.office' },
      { key: 'variance_report', label: 'Variance Report', icon: 'pi pi-chart-bar', to: '/variance-report', type: 'Invoices', permission: 'nav.admin' },
      { key: 'forecasting', label: 'Forecasting', icon: 'pi pi-chart-line', to: '/forecasting', type: 'Invoices', permission: 'nav.admin' },
      { key: 'budget', label: 'Budget', icon: 'pi pi-calculator', to: '/budget', type: 'Invoices', permission: 'accounting.read' },
      { key: 'spending_trends', label: 'Spending Trends', icon: 'pi pi-chart-line', to: '/spending-trends', type: 'Invoices', permission: 'accounting.read' },
      { key: 'overhead', label: 'Overhead', icon: 'pi pi-calculator', to: '/overhead', type: 'Invoices', permission: 'accounting.read' },
      // 2026-07-01: /admin/games was an orphan route (reachable only by URL).
      // Surfaced here per Doug — future release, gamified motivation.
      {
        key: 'games',
        label: 'Games — Future Release',
        icon: 'pi pi-trophy',
        to: '/admin/games',
        type: 'Operations',
        permission: 'nav.admin',
        description: 'Coming soon: a way to motivate everyone to do their job correctly. Game Theory for motivation.',
      },
    ],
  },
  {
    key: 'marketing',
    label: 'Marketing',
    icon: 'pi pi-megaphone',
    modules: [
      { key: 'campaigns', label: 'Campaigns', icon: 'pi pi-megaphone', to: '/campaigns', type: 'Jobs', permission: 'nav.office', cluster: 'marketing_hub', tabLabel: 'Campaigns' },
      { key: 'segments', label: 'Segments', icon: 'pi pi-sliders-h', to: '/segments', type: 'Customers', permission: 'nav.office', cluster: 'marketing_hub', tabLabel: 'Segments' },
      { key: 'automations', label: 'Automations', icon: 'pi pi-bolt', to: '/automations', type: 'Customers', permission: 'nav.office', cluster: 'marketing_hub', tabLabel: 'Automations' },
      { key: 'winback', label: 'Winback & Follow-ups', icon: 'pi pi-refresh', to: '/winback', type: 'Customers', permission: 'nav.office', cluster: 'marketing_hub', tabLabel: 'Winback' },
      { key: 'loyalty', label: 'Loyalty', icon: 'pi pi-star', to: '/loyalty', type: 'Customers', permission: 'nav.office', cluster: 'marketing_hub', tabLabel: 'Loyalty' },
    ],
  },
  {
    key: 'inventory',
    label: 'Inventory',
    icon: 'pi pi-box',
    modules: [
      { key: 'catalog', label: 'Catalog', icon: 'pi pi-list', to: '/catalog', type: 'Jobs', permission: 'nav.office' },
      // Field tier (ungated): inventory (read-only "do we have this part?").
      { key: 'inventory', label: 'Inventory', icon: 'pi pi-box', to: '/inventory', type: 'Jobs' },
      { key: 'parts_to_order', label: 'Parts to Order', icon: 'pi pi-box', to: '/parts-to-order', type: 'Jobs', permission: 'nav.office' },
      { key: 'vendors', label: 'Vendors', icon: 'pi pi-building', to: '/vendors', type: 'Jobs', permission: 'nav.office' },
      { key: 'purchase_orders', label: 'Purchase Orders', icon: 'pi pi-shopping-cart', to: '/purchase-orders', type: 'Jobs', permission: 'nav.office' },
    ],
  },
  {
    key: 'documents',
    label: 'Documents',
    icon: 'pi pi-folder',
    modules: [
      { key: 'documents', label: 'Documents', icon: 'pi pi-file', to: '/documents', type: 'Invoices', permission: 'nav.office' },
      { key: 'pdf_templates', label: 'PDF Templates', icon: 'pi pi-file-pdf', to: '/pdf-templates', type: 'Admin', permission: 'nav.office' },
      { key: 'resources', label: 'Resources', icon: 'pi pi-folder-open', to: '/resources', type: 'Admin', permission: 'nav.office' },
    ],
  },
  {
    key: 'admin',
    label: 'Admin',
    icon: 'pi pi-shield',
    modules: [
      { key: 'users', label: 'Users', icon: 'pi pi-user', to: '/users', type: 'Admin', permission: 'users.read' },
      { key: 'role_permissions', label: 'Roles & Permissions', icon: 'pi pi-lock', to: '/role-permissions', type: 'Admin', permission: 'settings.write' },
      { key: 'tags', label: 'Tags', icon: 'pi pi-tags', to: '/tags', type: 'Customers', permission: 'nav.office' },
      { key: 'custom_fields', label: 'Custom Fields', icon: 'pi pi-sliders-h', to: '/custom-fields', type: 'Admin', permission: 'settings.write' },
      { key: 'webhooks', label: 'Webhooks', icon: 'pi pi-bell', to: '/webhooks', type: 'Operations', permission: 'webhooks.manage' },
      { key: 'gdpr', label: 'GDPR & Compliance', icon: 'pi pi-shield', to: '/gdpr', type: 'Admin', permission: 'settings.write' },
      { key: 'activity', label: 'Activity', icon: 'pi pi-history', to: '/activity', type: 'Admin', permission: 'nav.admin' },
      { key: 'sso', label: 'SSO', icon: 'pi pi-lock', to: '/sso', type: 'Admin', permission: 'settings.write' },
      { key: 'onboarding', label: 'Onboarding', icon: 'pi pi-flag', to: '/onboarding', type: 'Admin', permission: 'nav.admin' },
      { key: 'admin_ops', label: 'Admin Operations', icon: 'pi pi-server', to: '/admin-ops', type: 'Admin', permission: 'settings.write' },
      { key: 'server_errors', label: 'Server Logs', icon: 'pi pi-exclamation-triangle', to: '/server-errors', type: 'Admin', permission: 'settings.write' },
      { key: 'admin_db', label: 'Database', icon: 'pi pi-database', to: '/admin/database', type: 'Admin', permission: 'settings.write' },
      // 2026-07-01 UX audit: key was 'payroll', colliding with the Payroll
      // category's module (two entries, one enablement key). Renamed key +
      // label so both can be granted/labeled independently.
      { key: 'admin_payroll', label: 'Payroll Admin', icon: 'pi pi-wallet', to: '/admin/payroll', type: 'Admin', permission: 'payroll.read', description: 'Payroll entries & configuration (admin)' },
      // 2026-07-01 UX audit: /feedback was an orphan route (no nav entry).
      { key: 'feedback_portal', label: 'Feedback Portal', icon: 'pi pi-comment', to: '/feedback', type: 'Admin', permission: 'nav.admin', description: 'Review feedback and bug reports submitted in-app' },
      { key: 'settings', label: 'Settings', icon: 'pi pi-cog', to: '/settings', type: 'Customers', permission: 'settings.read' },
    ],
  },
];

// One sidebar row per cluster; the row's target/active-state and the in-page
// tab bar are derived from the visible child modules (see useTenantModules
// `collapseClusters` and components/ModuleTabsPage.vue). A cluster row is
// visible iff at least one child survives enablement + permission filtering.
export const NAV_CLUSTERS = [
  { key: 'phone_hub', label: 'Phone', icon: 'pi pi-phone', description: 'Phone.com calls, SMS, cold leads & faxes' },
  { key: 'reputation_hub', label: 'Reputation', icon: 'pi pi-star', description: 'Reviews, referrals & surveys' },
  { key: 'billing_hub', label: 'Billing', icon: 'pi pi-dollar', description: 'Invoices, payments, collections & reminders' },
  { key: 'marketing_hub', label: 'Marketing', icon: 'pi pi-megaphone', description: 'Campaigns, segments, automations, winback & loyalty' },
  { key: 'fleet_hub', label: 'Fleet', icon: 'pi pi-truck', description: 'Vehicles, live GPS & coverage map' },
  { key: 'loadsheets_hub', label: 'Load Sheets', icon: 'pi pi-check-square', description: 'Daily & delivery load sheets' },
];

export function clusterByKey(key) {
  return NAV_CLUSTERS.find((c) => c.key === key) || null;
}

export const QUICK_ACTIONS = [
  { key: 'create-job', label: 'Create Job', icon: 'pi pi-plus-circle', to: '/jobs', type: 'Quick Actions' },
  {
    key: 'create-customer',
    label: 'Create Customer',
    icon: 'pi pi-user-plus',
    to: '/customers',
    type: 'Quick Actions',
  },
  { key: 'open-dispatch', label: 'Open Dispatch', icon: 'pi pi-map', to: '/dispatch', type: 'Quick Actions' },
];

export function flattenModules(categories = MODULE_CATEGORIES) {
  return categories.flatMap((category) => category.modules);
}

export function titleFromKey(key) {
  return key
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}
