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

export const MODULE_CATEGORIES = [
  {
    key: 'operations',
    label: 'Operations',
    icon: 'pi pi-cog',
    modules: [
      // Field tier (ungated — every role, no permission needed): jobs, timeclock, photos.
      { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/jobs', type: 'Jobs' },
      { key: 'dispatch', label: 'Dispatch', icon: 'pi pi-map', to: '/dispatch', type: 'Jobs', permission: 'nav.office' },
      { key: 'scheduling', label: 'Scheduling', icon: 'pi pi-calendar', to: '/scheduling', type: 'Operations', permission: 'nav.office' },
      { key: 'appointments', label: 'Appointments', icon: 'pi pi-calendar-plus', to: '/appointments', type: 'Jobs', permission: 'nav.office' },
      { key: 'tasks', label: 'Tasks', icon: 'pi pi-list', to: '/tasks', type: 'Operations', permission: 'nav.office' },
      { key: 'planner', label: 'Planner', icon: 'pi pi-calendar-plus', to: '/planner', type: 'Operations', permission: 'nav.office' },
      { key: 'checklists', label: 'Checklists', icon: 'pi pi-check-square', to: '/checklists', type: 'Operations', permission: 'nav.office' },
      { key: 'job_templates', label: 'Job Templates', icon: 'pi pi-th-large', to: '/job-templates', type: 'Operations', permission: 'nav.office' },
      { key: 'maintenance', label: 'Maintenance Plans', icon: 'pi pi-wrench', to: '/maintenance', type: 'Jobs', permission: 'nav.office' },
      { key: 'technicians', label: 'Technicians', icon: 'pi pi-users', to: '/technicians', type: 'Operations', permission: 'nav.office' },
      { key: 'performance', label: 'Performance', icon: 'pi pi-chart-line', to: '/performance', type: 'Operations', permission: 'nav.office' },
      { key: 'timeclock', label: 'Timeclock', icon: 'pi pi-clock', to: '/timeclock', type: 'Operations' },
      { key: 'fleet', label: 'Fleet', icon: 'pi pi-truck', to: '/fleet', type: 'Operations', permission: 'nav.office' },
      { key: 'gps', label: 'GPS', icon: 'pi pi-compass', to: '/gps', type: 'Operations', permission: 'nav.office' },
      { key: 'maps', label: 'Maps', icon: 'pi pi-globe', to: '/maps', type: 'Operations', permission: 'nav.office' },
      { key: 'daily_loadsheet', label: 'Daily Load Sheet', icon: 'pi pi-check-square', to: '/daily-loadsheet', type: 'Operations', permission: 'nav.office' },
      { key: 'delivery_loadsheet', label: 'Delivery Load Sheet', icon: 'pi pi-truck', to: '/delivery-loadsheet', type: 'Operations', permission: 'nav.office' },
      { key: 'equipment', label: 'Customer Equipment', icon: 'pi pi-cog', to: '/equipment', type: 'Operations', permission: 'nav.office' },
      { key: 'equipment_tracking', label: 'Company Tools', icon: 'pi pi-database', to: '/equipment-tracking', type: 'Operations', permission: 'nav.office' },
      { key: 'photos', label: 'Photos', icon: 'pi pi-images', to: '/photos', type: 'Jobs' },
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
      { key: 'communications', label: 'Communications', icon: 'pi pi-comments', to: '/communications', type: 'Customers' },
      { key: 'inbox', label: 'Inbox', icon: 'pi pi-inbox', to: '/inbox', type: 'Operations' },
      { key: 'phone_com_calls', label: 'Phone.com Calls', icon: 'pi pi-phone', to: '/phone-com/calls', type: 'Customers', requires: 'phone_com', permission: 'nav.office' },
      { key: 'phone_com_messages', label: 'Phone.com SMS', icon: 'pi pi-comment', to: '/phone-com/messages', type: 'Customers', requires: 'phone_com', permission: 'nav.office' },
      { key: 'phone_com_faxes', label: 'Phone.com Faxes', icon: 'pi pi-file-pdf', to: '/phone-com/faxes', type: 'Customers', requires: 'phone_com', permission: 'nav.office' },
      { key: 'reviews', label: 'Reviews', icon: 'pi pi-star', to: '/reviews', type: 'Customers', permission: 'nav.office' },
      { key: 'referrals', label: 'Referrals', icon: 'pi pi-share-alt', to: '/referrals', type: 'Customers', permission: 'nav.office' },
      { key: 'surveys', label: 'Surveys', icon: 'pi pi-comments', to: '/surveys', type: 'Customers', permission: 'nav.office' },
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
  {
    key: 'financials',
    label: 'Financials',
    icon: 'pi pi-dollar',
    modules: [
      { key: 'billing', label: 'Billing', icon: 'pi pi-dollar', to: '/billing', type: 'Invoices', permission: 'invoices.read_all' },
      { key: 'payments', label: 'Payments', icon: 'pi pi-credit-card', to: '/payments', type: 'Invoices', permission: 'payments.read' },
      { key: 'expenses', label: 'Expenses', icon: 'pi pi-wallet', to: '/expenses', type: 'Invoices', permission: 'accounting.read' },
      { key: 'collections', label: 'Collections', icon: 'pi pi-wallet', to: '/collections', type: 'Invoices', permission: 'invoices.read_all' },
      { key: 'invoice_reminders', label: 'Invoice Reminders', icon: 'pi pi-bell', to: '/invoice-reminders', type: 'Invoices', permission: 'invoices.read_all' },
      { key: 'payroll', label: 'Payroll', icon: 'pi pi-money-bill', to: '/payroll', type: 'Invoices', permission: 'payroll.read' },
      { key: 'commissions', label: 'Commissions', icon: 'pi pi-percentage', to: '/commissions', type: 'Invoices', permission: 'nav.admin' },
      { key: 'job_costing', label: 'Job Costing', icon: 'pi pi-chart-line', to: '/job-costing', type: 'Invoices', permission: 'nav.admin' },
      { key: 'pricing', label: 'Pricing', icon: 'pi pi-tags', to: '/pricing', type: 'Invoices', permission: 'nav.admin' },
      { key: 'labor_matrix', label: 'Labor Matrix', icon: 'pi pi-wrench', to: '/labor-matrix', type: 'Invoices', permission: 'pricing.labor_matrix.read' },
      { key: 'vendor_statements', label: 'Vendor Statements', icon: 'pi pi-file-import', to: '/vendor-statements', type: 'Invoices', permission: 'vendor_statements.read' },
      { key: 'exports', label: 'Exports', icon: 'pi pi-download', to: '/exports', type: 'Invoices', permission: 'nav.admin' },
      { key: 'quickbooks', label: 'QuickBooks', icon: 'pi pi-plug', to: '/quickbooks', type: 'Invoices', permission: 'nav.admin' },
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
    ],
  },
  {
    key: 'marketing',
    label: 'Marketing',
    icon: 'pi pi-megaphone',
    modules: [
      { key: 'campaigns', label: 'Campaigns', icon: 'pi pi-megaphone', to: '/campaigns', type: 'Jobs', permission: 'nav.office' },
      { key: 'segments', label: 'Segments', icon: 'pi pi-sliders-h', to: '/segments', type: 'Customers', permission: 'nav.office' },
      { key: 'automations', label: 'Automations', icon: 'pi pi-bolt', to: '/automations', type: 'Customers', permission: 'nav.office' },
      { key: 'winback', label: 'Winback & Follow-ups', icon: 'pi pi-refresh', to: '/winback', type: 'Customers', permission: 'nav.office' },
      { key: 'loyalty', label: 'Loyalty', icon: 'pi pi-star', to: '/loyalty', type: 'Customers', permission: 'nav.office' },
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
      { key: 'payroll', label: 'Payroll', icon: 'pi pi-wallet', to: '/admin/payroll', type: 'Admin', permission: 'payroll.read' },
      { key: 'settings', label: 'Settings', icon: 'pi pi-cog', to: '/settings', type: 'Customers', permission: 'settings.read' },
    ],
  },
];

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
