/**
 * useModuleSections — mobile "More" drawer grouping + permission-driven
 * nav visibility.
 *
 * Takes the flat `allEnabledModules` list from `useTenantModules()` (already
 * permission-filtered there) and returns it grouped, search-filtered, and
 * de-duplicated for the mobile "More" drawer.
 *
 * Nav visibility is now a SINGLE permission-driven source of truth. Each module
 * in constants/modules.js declares a `permission`; a user sees it iff they hold
 * that permission (`moduleVisible` below). Modules with NO permission are field
 * tier — visible to every role. The old hardcoded FIELD_TECH_MODULES /
 * OFFICE_MODULES Sets (which decided visibility by role STRING, parallel to and
 * sometimes conflicting with permissions) were deleted in this migration; their
 * intent now lives in the `nav.office` / `nav.admin` permissions (see
 * core/permissions.py) plus each module's existing fine-grained permission.
 *
 * Section policy (deterministic order):
 *   Field · Customers & Comms · Money · Inventory · Admin · Account · Other
 * Anything not explicitly assigned falls into "Other", rendered last.
 *
 * De-duplication: by `(label, permission)` pair. First occurrence wins.
 */

/**
 * Single source of truth for "should this module appear in nav for this user".
 * @param {{permission?: string}} module — a module catalog entry
 * @param {(key: string) => boolean} hasPermission — auth store's hasPermission
 * @returns {boolean} ungated (no permission) → always visible; else gated.
 */
export function moduleVisible(module, hasPermission) {
  if (!module || !module.permission) return true;
  return hasPermission(module.permission);
}

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
 * Visibility is NOT decided here — `modules` is expected to already be
 * permission-filtered upstream (useTenantModules `allEnabledModules`). This
 * function only de-dupes, search-filters, and buckets into sections.
 *
 * @param {Array} modules    — enriched module entries: { key, label, icon, to, permission? }
 * @param {string} [search]  — optional filter string from the drawer's search input
 * @returns {Array<{ section: string, modules: Array }>}
 *   Sections appear in SECTION_ORDER; empty sections are dropped.
 */
export function groupModules(modules, search = '') {
  const filtered = _dedupe(
    (modules || []).filter((m) => _matchesSearch(m, search)),
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
  // current SECTION_BY_KEY map but keeps a future "Settings" or
  // "Integrations" section showing).
  for (const [name, list] of buckets) {
    if (SECTION_ORDER.includes(name)) continue;
    if (list.length) sections.push({ section: name, modules: list });
  }
  return sections;
}
