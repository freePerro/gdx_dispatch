/**
 * useModuleSections — MH-4 (mobile hardening sprint).
 *
 * Takes the flat `allEnabledModules` list from `useTenantModules()` and
 * returns it grouped, role-filtered, search-filtered, and de-duplicated
 * for the mobile "More" drawer.
 *
 * Audit P1 #5: the drawer was rendering all ~80 modules in one flat
 * list with no grouping, no role gate, no search — Payroll/QuickBooks/
 * Webhooks/Feature Flags/GDPR/SSO/Admin Ops all tappable by a field
 * tech on their phone. P2 #22: "Payroll" appeared twice (one
 * `/payroll`, one `/admin/payroll`) with identical label.
 *
 * Allowlist policy:
 *   - `tech`/`technician`: small set of field-relevant modules. Hide
 *     everything else (admin/finance/marketing).
 *   - `dispatcher`/`sales`/`office`/any non-admin/non-owner non-tech
 *     role: office set adds customer + comms + money basics, still
 *     hides platform-admin items.
 *   - `admin`/`owner`/`super_admin`: full list — the existing power-user
 *     contract.
 *
 * Section policy (5 buckets, deterministic order):
 *   Field · Customers & Comms · Money · Inventory · Admin
 * Anything not explicitly assigned falls into a sixth "Other" bucket
 * rendered last (defensive — a future module key without a section
 * still shows, just at the bottom).
 *
 * De-duplication: by `(label, permission)` pair. The first occurrence
 * in the source list wins; subsequent matches are dropped silently.
 */

// Module keys visible to field techs. MH-9: trimmed from 21 to 6 after
// research on ServiceTitan / Jobber / Housecall Pro tech apps (the
// "always-visible core" is 5-8 items; everything else lives inside a
// per-job drilldown). Anything dropped from here MUST be added to
// OFFICE_MODULES below or office roles regress.
//   Kept: jobs (bottom-nav, listed for completeness — reservedKeys
//   filters it from More), timeclock (also bottom-nav), photos (before/
//   after upload), inbox + communications (ping dispatch), inventory
//   (read-only "do we have this part?").
export const FIELD_TECH_MODULES = new Set([
  'jobs',
  'timeclock',
  'photos',
  'inbox', 'communications',
  'inventory',
  // MH-9b: Profile is injected into moreModules for techs (it's not in
  // the module catalog) and needs to pass the role allowlist to render.
  'profile',
]);

// Additive on top of FIELD_TECH_MODULES for office roles. MH-9: absorbed
// the 15 keys dropped from FIELD_TECH_MODULES so office (dispatcher /
// sales / etc.) keeps full visibility into scheduling / planner /
// dispatch / maps / parts ordering / equipment / customer portal.
export const OFFICE_MODULES = new Set([
  // Field tools the office needs but techs no longer see.
  'dispatch', 'scheduling', 'appointments', 'tasks', 'planner',
  'checklists', 'job_templates', 'maintenance', 'technicians',
  'maps', 'gps', 'daily_loadsheet', 'equipment', 'parts_to_order',
  'customer_portal',
  // Customers + money + comms.
  'customers', 'leads', 'estimates', 'proposals',
  'change_orders', 'service_agreements', 'signatures',
  'billing', 'payments', 'collections', 'invoice_reminders',
  'reviews', 'referrals', 'surveys', 'booking', 'warranties',
  'phone_com_calls', 'phone_com_messages', 'phone_com_faxes',
  'campaigns', 'segments', 'automations', 'winback', 'loyalty',
  'fleet', 'performance', 'equipment_tracking', 'delivery_loadsheet',
  'documents', 'pdf_templates', 'resources', 'reports', 'tags',
  'catalog', 'vendors', 'purchase_orders',
]);

// Section assignment per module key. Anything not listed → "Other".
const SECTION_BY_KEY = {
  // Field
  jobs: 'Field', dispatch: 'Field', scheduling: 'Field',
  appointments: 'Field', tasks: 'Field', planner: 'Field',
  checklists: 'Field', job_templates: 'Field',
  maintenance: 'Field', technicians: 'Field', performance: 'Field',
  timeclock: 'Field', fleet: 'Field', gps: 'Field', maps: 'Field',
  daily_loadsheet: 'Field', delivery_loadsheet: 'Field',
  equipment: 'Field', equipment_tracking: 'Field',
  photos: 'Field',
  // Customers & Comms
  customers: 'Customers & Comms', customer_portal: 'Customers & Comms',
  communications: 'Customers & Comms', inbox: 'Customers & Comms',
  phone_com_calls: 'Customers & Comms',
  phone_com_messages: 'Customers & Comms',
  phone_com_faxes: 'Customers & Comms',
  reviews: 'Customers & Comms', referrals: 'Customers & Comms',
  surveys: 'Customers & Comms', booking: 'Customers & Comms',
  warranties: 'Customers & Comms', leads: 'Customers & Comms',
  campaigns: 'Customers & Comms', segments: 'Customers & Comms',
  automations: 'Customers & Comms', winback: 'Customers & Comms',
  loyalty: 'Customers & Comms', tags: 'Customers & Comms',
  // Money
  estimates: 'Money', proposals: 'Money',
  change_orders: 'Money', service_agreements: 'Money',
  signatures: 'Money', billing: 'Money', payments: 'Money',
  expenses: 'Money', collections: 'Money',
  invoice_reminders: 'Money', payroll: 'Money',
  commissions: 'Money', job_costing: 'Money',
  pricing: 'Money', labor_matrix: 'Money',
  vendor_statements: 'Money', reports: 'Money',
  variance_report: 'Money', exports: 'Money',
  quickbooks: 'Money',
  // Inventory
  catalog: 'Inventory', inventory: 'Inventory',
  parts_to_order: 'Inventory', vendors: 'Inventory',
  purchase_orders: 'Inventory',
  // Admin
  documents: 'Admin', pdf_templates: 'Admin', resources: 'Admin',
  users: 'Admin', role_permissions: 'Admin', custom_fields: 'Admin',
  webhooks: 'Admin', gdpr: 'Admin',
  activity: 'Admin', sso: 'Admin', onboarding: 'Admin',
  admin_operations: 'Admin', settings: 'Admin',
  // Account (MH-9b)
  profile: 'Account',
};

export const SECTION_ORDER = [
  'Field',
  'Customers & Comms',
  'Money',
  'Inventory',
  'Admin',
  'Account',
  'Other',
];

function _normalizeRole(role) {
  const r = String(role || '').toLowerCase();
  if (r === 'technician') return 'tech';
  return r;
}

function _isAllowedForRole(moduleKey, role) {
  const r = _normalizeRole(role);
  if (r === 'admin' || r === 'owner' || r === 'super_admin') return true;
  if (r === 'tech') return FIELD_TECH_MODULES.has(moduleKey);
  // Office / dispatcher / sales / unknown non-tech non-admin → office set.
  return FIELD_TECH_MODULES.has(moduleKey) || OFFICE_MODULES.has(moduleKey);
}

// DT-1: same role allowlist exposed so desktop sidebar + CommandPalette
// can gate their non-section-grouped surfaces without re-implementing the
// rule. Mobile More drawer goes through `groupModulesForRole` which already
// uses `_isAllowedForRole` internally.
export function isModuleAllowedForRole(moduleKey, role) {
  return _isAllowedForRole(moduleKey, role);
}

function _dedupe(modules) {
  const seen = new Set();
  const out = [];
  for (const m of modules) {
    // Drop the catalog's TWO `payroll` entries (one /payroll, one
    // /admin/payroll). De-dupe on (label, permission) — first wins.
    const fingerprint = `${m.label}::${m.permission || ''}`;
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    out.push(m);
  }
  return out;
}

function _matchesSearch(module, query) {
  if (!query) return true;
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if ((module.label || '').toLowerCase().includes(q)) return true;
  if ((module.key || '').toLowerCase().includes(q)) return true;
  if ((module.to || '').toLowerCase().includes(q)) return true;
  return false;
}

/**
 * Group a flat module list into sections for the More drawer.
 *
 * @param {Array} modules    — enriched module entries: { key, label, icon, to, permission? }
 * @param {string} role      — auth.user.role (case-insensitive)
 * @param {string} [search]  — optional filter string from the drawer's search input
 * @returns {Array<{ section: string, modules: Array }>}
 *   Sections appear in SECTION_ORDER; empty sections are dropped.
 */
export function groupModulesForRole(modules, role, search = '') {
  const filtered = _dedupe(
    (modules || []).filter((m) => _isAllowedForRole(m.key, role) && _matchesSearch(m, search)),
  );
  const buckets = new Map();
  for (const name of SECTION_ORDER) buckets.set(name, []);
  for (const m of filtered) {
    const section = SECTION_BY_KEY[m.key] || 'Other';
    if (!buckets.has(section)) buckets.set(section, []);
    buckets.get(section).push(m);
  }
  const sections = [];
  for (const name of SECTION_ORDER) {
    const list = buckets.get(name) || [];
    if (list.length) sections.push({ section: name, modules: list });
  }
  // Sections not in SECTION_ORDER (defensive — shouldn't happen with the
  // current `_normalizeRole` paths but keeps a future "Settings" or
  // "Integrations" section showing).
  for (const [name, list] of buckets) {
    if (SECTION_ORDER.includes(name)) continue;
    if (list.length) sections.push({ section: name, modules: list });
  }
  return sections;
}
