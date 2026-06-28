/**
 * Nav visibility — Set→permission migration guard + grouping behaviour.
 *
 * The old hardcoded FIELD_TECH_MODULES / OFFICE_MODULES Sets (which decided nav
 * visibility by ROLE STRING) were deleted. Visibility is now a single
 * permission-driven source of truth: each module declares a `permission` and a
 * user sees it iff they hold it (ungated modules = field tier, visible to all).
 *
 * This suite freezes the pre-migration Set behaviour as a snapshot and asserts
 * the new permission model:
 *   1. loses NOTHING for any builtin role (zero regressions), and
 *   2. differs ONLY by the 19 approved "reveals" — modules a role already had
 *      the API permission for but the Set was inconsistently hiding from nav.
 */
import { describe, it, expect } from 'vitest';
import { groupModules, moduleVisible, SECTION_ORDER } from '../useModuleSections';
import { MODULE_CATEGORIES, flattenModules } from '../../constants/modules';

// ── Resolved builtin-role permissions. MIRROR of core/permissions.py
//    BUILTIN_ROLES (keep in sync; regenerate with the dump in that file's
//    docstring if roles change). 'owner' = wildcard. ────────────────────────
const RESOLVED_ROLE_PERMS = {
  owner: '*',
  admin: ['accounting.export','accounting.read','accounting.write','billing.read','customers.delete','customers.read_all','customers.read_own','customers.write','dispatch.read','estimates.read_all','estimates.read_own','estimates.send','estimates.write','inventory.read','inventory.write','invoices.read_all','invoices.read_own','invoices.refund','invoices.send','invoices.write','jobs.delete','jobs.read_all','jobs.read_own','jobs.write','leads.delete','leads.read','leads.write','mobile.chat','mobile.dispatch_view','mobile.use','nav.admin','nav.office','payments.process','payments.read','payroll.export','payroll.read','payroll.write','pricing.labor_matrix.read','pricing.labor_matrix.write','reports.export','reports.read','scheduling.read_all','scheduling.read_own','scheduling.write','settings.read','settings.write','users.read','users.write','vendor_statements.read','vendor_statements.write','webhooks.manage'],
  dispatcher: ['customers.read_all','customers.write','dispatch.read','estimates.read_all','estimates.write','invoices.read_all','jobs.read_all','jobs.write','leads.delete','leads.read','leads.write','mobile.chat','mobile.dispatch_view','mobile.use','nav.office','payments.read','pricing.labor_matrix.read','pricing.labor_matrix.write','scheduling.read_all','scheduling.write','vendor_statements.read','vendor_statements.write'],
  technician: ['customers.read_own','estimates.read_own','inventory.read','inventory.write','jobs.read_own','jobs.write','mobile.chat','mobile.use','pricing.labor_matrix.read','scheduling.read_own'],
  sales: ['customers.read_all','customers.write','estimates.read_all','estimates.send','estimates.write','invoices.read_all','jobs.read_all','leads.delete','leads.read','leads.write','nav.office','pricing.labor_matrix.read','pricing.labor_matrix.write','vendor_statements.read'],
  accounting: ['accounting.export','accounting.read','accounting.write','billing.read','invoices.read_all','invoices.refund','invoices.send','invoices.write','leads.read','nav.office','payments.process','payments.read','payroll.export','payroll.read','payroll.write','pricing.labor_matrix.read','pricing.labor_matrix.write','reports.export','reports.read','vendor_statements.read','vendor_statements.write'],
  viewer: ['accounting.read','billing.read','customers.read_all','customers.read_own','dispatch.read','estimates.read_all','estimates.read_own','inventory.read','invoices.read_all','invoices.read_own','jobs.read_all','jobs.read_own','leads.read','nav.office','payments.read','payroll.read','pricing.labor_matrix.read','reports.read','scheduling.read_all','scheduling.read_own','settings.read','users.read','vendor_statements.read'],
};

// ── Frozen pre-migration snapshot (the deleted Sets + the module→permission
//    map as it was BEFORE tagging). This is the "before" we migrated from. ───
const OLD_FIELD = new Set(['jobs', 'timeclock', 'photos', 'inbox', 'communications', 'inventory']);
const OLD_OFFICE = new Set([
  'dispatch', 'scheduling', 'appointments', 'tasks', 'planner', 'checklists',
  'job_templates', 'maintenance', 'technicians', 'maps', 'gps', 'daily_loadsheet',
  'equipment', 'parts_to_order', 'customer_portal', 'customers', 'leads', 'estimates',
  'proposals', 'change_orders', 'service_agreements', 'signatures', 'billing', 'payments',
  'collections', 'invoice_reminders', 'reviews', 'referrals', 'surveys', 'booking',
  'warranties', 'phone_com_calls', 'phone_com_messages', 'phone_com_faxes', 'campaigns',
  'segments', 'automations', 'winback', 'loyalty', 'fleet', 'performance',
  'equipment_tracking', 'delivery_loadsheet', 'documents', 'pdf_templates', 'resources',
  'reports', 'tags', 'catalog', 'vendors', 'purchase_orders',
]);
const OLD_MODULE_PERM = {
  billing: 'invoices.read_all', payments: 'payments.read', expenses: 'accounting.read',
  collections: 'invoices.read_all', invoice_reminders: 'invoices.read_all', payroll: 'payroll.read',
  labor_matrix: 'pricing.labor_matrix.read', vendor_statements: 'vendor_statements.read',
  budget: 'accounting.read', spending_trends: 'accounting.read', users: 'users.read',
  role_permissions: 'settings.write', custom_fields: 'settings.write', webhooks: 'webhooks.manage',
  gdpr: 'settings.write', sso: 'settings.write', admin_ops: 'settings.write',
  server_errors: 'settings.write', admin_db: 'settings.write', settings: 'settings.read',
};

// The 21 approved reveals (see migration decision: "Reveal them"). Each is a
// role that already holds the API permission but the old Set hid the nav.
// overhead (ADR-016) is accounting.read-gated like budget/spending_trends, so it
// reveals to the same roles (accounting, viewer).
const APPROVED_REVEALS = new Set([
  'dispatcher:labor_matrix', 'dispatcher:vendor_statements',
  'technician:labor_matrix',
  'sales:labor_matrix', 'sales:vendor_statements',
  'accounting:expenses', 'accounting:payroll', 'accounting:labor_matrix',
  'accounting:vendor_statements', 'accounting:budget', 'accounting:spending_trends',
  'accounting:overhead',
  'viewer:expenses', 'viewer:payroll', 'viewer:labor_matrix', 'viewer:vendor_statements',
  'viewer:budget', 'viewer:spending_trends', 'viewer:users', 'viewer:settings',
  'viewer:overhead',
]);

const PARITY_ROLES = ['owner', 'admin', 'dispatcher', 'technician', 'sales', 'accounting', 'viewer'];
const MODULES = flattenModules();

function canonical(role) {
  const r = String(role || '').toLowerCase();
  if (r === 'tech' || r === 'technician') return 'technician';
  if (r === 'dispatch' || r === 'dispatcher') return 'dispatcher';
  return r;
}
function roleHas(role, perm) {
  const perms = RESOLVED_ROLE_PERMS[canonical(role)] || [];
  if (perms === '*') return true;
  return perms.includes(perm);
}
function oldSetAllowed(role, key) {
  const r = canonical(role);
  if (r === 'admin' || r === 'owner' || r === 'super_admin') return true;
  if (r === 'technician') return OLD_FIELD.has(key);
  return OLD_FIELD.has(key) || OLD_OFFICE.has(key);
}
function oldVisible(role, key) {
  if (!oldSetAllowed(role, key)) return false;
  const perm = OLD_MODULE_PERM[key];
  return perm ? roleHas(role, perm) : true;
}
function newVisible(role, module) {
  return moduleVisible(module, (k) => roleHas(role, k));
}

describe('nav visibility — Set→permission migration parity', () => {
  it('zero regressions: no builtin role loses any module it could see before', () => {
    const regressions = [];
    for (const role of PARITY_ROLES) {
      for (const m of MODULES) {
        if (oldVisible(role, m.key) && !newVisible(role, m)) regressions.push(`${role}:${m.key}`);
      }
    }
    expect(regressions).toEqual([]);
  });

  it('differs from the old Sets only by the 19 approved reveals', () => {
    const reveals = new Set();
    for (const role of PARITY_ROLES) {
      for (const m of MODULES) {
        if (!oldVisible(role, m.key) && newVisible(role, m)) reveals.add(`${role}:${m.key}`);
      }
    }
    expect([...reveals].sort()).toEqual([...APPROVED_REVEALS].sort());
  });
});

describe('moduleVisible — single permission-driven gate', () => {
  it('ungated module (no permission) is visible to everyone', () => {
    expect(moduleVisible({ key: 'jobs' }, () => false)).toBe(true);
  });
  it('gated module is hidden without the permission', () => {
    expect(moduleVisible({ key: 'quickbooks', permission: 'nav.admin' }, () => false)).toBe(false);
  });
  it('gated module is shown with the permission', () => {
    expect(moduleVisible({ key: 'quickbooks', permission: 'nav.admin' }, (k) => k === 'nav.admin')).toBe(true);
  });
});

describe('catalog tagging — tier invariants', () => {
  const FIELD = new Set(['jobs', 'timeclock', 'photos', 'communications', 'inbox', 'inventory']);

  it('the only ungated modules are the 6 field-tier ones', () => {
    const ungated = MODULES.filter((m) => !m.permission).map((m) => m.key);
    expect(new Set(ungated)).toEqual(FIELD);
  });

  it('technician (field tier) sees no nav.office or nav.admin gated module', () => {
    for (const m of MODULES) {
      if (newVisible('technician', m)) {
        expect(['nav.office', 'nav.admin']).not.toContain(m.permission);
      }
    }
  });

  it('office roles see nav.office modules but not nav.admin modules', () => {
    const scheduling = MODULES.find((m) => m.key === 'scheduling'); // nav.office
    const quickbooks = MODULES.find((m) => m.key === 'quickbooks'); // nav.admin
    for (const role of ['dispatcher', 'sales', 'accounting', 'viewer']) {
      expect(newVisible(role, scheduling)).toBe(true);
      expect(newVisible(role, quickbooks)).toBe(false);
    }
  });

  it('admin and owner see every module', () => {
    for (const role of ['admin', 'owner']) {
      for (const m of MODULES) expect(newVisible(role, m)).toBe(true);
    }
  });
});

// ── grouping / search / dedupe (groupModules no longer role-gates) ──────────
const SYN = [
  { key: 'jobs', label: 'Jobs', icon: '', to: '/jobs' },
  { key: 'inbox', label: 'Inbox', icon: '', to: '/inbox' },
  { key: 'billing', label: 'Billing', icon: '', to: '/billing', permission: 'invoices.read_all' },
  { key: 'inventory', label: 'Inventory', icon: '', to: '/inventory' },
  { key: 'webhooks', label: 'Webhooks', icon: '', to: '/webhooks', permission: 'webhooks.manage' },
  // Duplicate payroll entries — must collapse to ONE (label+permission de-dupe).
  { key: 'payroll', label: 'Payroll', icon: '', to: '/payroll', permission: 'payroll.read' },
  { key: 'payroll', label: 'Payroll', icon: '', to: '/admin/payroll', permission: 'payroll.read' },
];

function allKeys(sections) {
  return sections.flatMap((s) => s.modules.map((m) => m.key));
}

describe('groupModules — grouping', () => {
  it('emits sections in SECTION_ORDER', () => {
    const names = groupModules(SYN).map((b) => b.section);
    const positions = names.map((n) => SECTION_ORDER.indexOf(n));
    for (let i = 1; i < positions.length; i++) {
      if (positions[i - 1] === -1 || positions[i] === -1) continue;
      expect(positions[i]).toBeGreaterThan(positions[i - 1]);
    }
  });

  it('drops empty sections', () => {
    const noMoney = SYN.filter((m) => !['billing', 'payroll'].includes(m.key));
    const names = groupModules(noMoney).map((b) => b.section);
    expect(names).not.toContain('Money');
  });

  it('routes a known key to its documented section', () => {
    const s = groupModules(SYN);
    const sectionOf = (k) => s.find((b) => b.modules.some((m) => m.key === k))?.section;
    expect(sectionOf('jobs')).toBe('Field');
    expect(sectionOf('inbox')).toBe('Customers & Comms');
    expect(sectionOf('billing')).toBe('Money');
    expect(sectionOf('inventory')).toBe('Inventory');
    expect(sectionOf('webhooks')).toBe('Admin');
  });

  it('lands an unknown key in "Other" rather than dropping it', () => {
    const s = groupModules([...SYN, { key: 'experimental_thing', label: 'X', icon: '', to: '/x' }]);
    const sectionOf = (k) => s.find((b) => b.modules.some((m) => m.key === k))?.section;
    expect(sectionOf('experimental_thing')).toBe('Other');
  });

  it('routes injected Profile to the Account section', () => {
    const s = groupModules([...SYN, { key: 'profile', label: 'Profile', icon: '', to: '/profile' }]);
    const sectionOf = (k) => s.find((b) => b.modules.some((m) => m.key === k))?.section;
    expect(sectionOf('profile')).toBe('Account');
  });
});

describe('groupModules — search', () => {
  it('case-insensitive label match', () => {
    expect(allKeys(groupModules(SYN, 'PAYR'))).toEqual(['payroll']);
  });
  it('matches by route too', () => {
    expect(allKeys(groupModules(SYN, '/webhooks'))).toEqual(['webhooks']);
  });
  it('returns an empty section list when no match', () => {
    expect(groupModules(SYN, 'zzznotamodule')).toEqual([]);
  });
  it('empty / whitespace query is treated as no filter', () => {
    const a = allKeys(groupModules(SYN, ''));
    const b = allKeys(groupModules(SYN, '   '));
    expect(a.length).toBeGreaterThan(0);
    expect(b.length).toBe(a.length);
  });
});

describe('groupModules — Payroll de-duplication', () => {
  it('collapses both Payroll entries into one (label+permission de-dupe)', () => {
    const payrolls = groupModules(SYN).flatMap((b) => b.modules).filter((m) => m.label === 'Payroll');
    expect(payrolls.length).toBe(1);
  });
  it('keeps the FIRST Payroll entry (canonical /payroll)', () => {
    const payroll = groupModules(SYN).flatMap((b) => b.modules).find((m) => m.label === 'Payroll');
    expect(payroll?.to).toBe('/payroll');
  });
});

describe('SECTION_ORDER invariant', () => {
  it('includes the 6 documented sections + "Other" fallback', () => {
    expect(SECTION_ORDER).toEqual([
      'Field', 'Customers & Comms', 'Money', 'Inventory', 'Admin', 'Account', 'Other',
    ]);
  });

  it('MODULE_CATEGORIES is non-empty (sanity)', () => {
    expect(MODULE_CATEGORIES.length).toBeGreaterThan(0);
  });
});
