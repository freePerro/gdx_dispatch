/**
 * MH-4 — Lock the "More" drawer role gate + grouping + search behaviour.
 *
 * Audit P1 #5: pre-fix the mobile More drawer rendered all ~80 enabled
 * modules with NO role filter — Payroll/Webhooks/Feature Flags/GDPR/
 * SSO/Admin Operations were all tappable by a field tech.
 * P2 #22: "Payroll" appeared twice (both /payroll and /admin/payroll).
 *
 * Tests cover:
 *   - role-allowlist (tech / office / admin) drops the right items
 *   - sections appear in SECTION_ORDER, empty sections elided
 *   - search filter matches label / key / route, case-insensitive
 *   - Payroll de-duped by (label, permission)
 *   - unknown module key lands in "Other" (defensive)
 */
import { describe, it, expect } from 'vitest';
import {
  groupModulesForRole,
  isModuleAllowedForRole,
  FIELD_TECH_MODULES,
  OFFICE_MODULES,
  SECTION_ORDER,
} from '../useModuleSections';

// Synthetic catalog mirroring the real shape from constants/modules.js,
// kept small + explicit so failures point at the right rule.
const CATALOG = [
  { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/jobs' },
  { key: 'timeclock', label: 'Timeclock', icon: 'pi pi-clock', to: '/timeclock' },
  { key: 'photos', label: 'Photos', icon: 'pi pi-images', to: '/photos' },
  { key: 'inventory', label: 'Inventory', icon: 'pi pi-box', to: '/inventory' },
  { key: 'inbox', label: 'Inbox', icon: 'pi pi-inbox', to: '/inbox' },
  { key: 'communications', label: 'Comms', icon: 'pi pi-comment', to: '/communications' },
  // MH-9: keys moved OUT of FIELD_TECH_MODULES — must show to office,
  // must NOT show to tech. Each one's tech-invisibility is asserted
  // below; one office-visibility check pins parts_to_order as the
  // representative.
  { key: 'parts_to_order', label: 'Parts to Order', icon: 'pi pi-box', to: '/parts-to-order' },
  { key: 'scheduling', label: 'Scheduling', icon: 'pi pi-calendar', to: '/scheduling' },
  { key: 'planner', label: 'Planner', icon: 'pi pi-calendar-plus', to: '/planner' },
  { key: 'dispatch', label: 'Dispatch', icon: 'pi pi-map', to: '/dispatch' },
  { key: 'maintenance', label: 'Maintenance', icon: 'pi pi-wrench', to: '/maintenance' },
  { key: 'technicians', label: 'Technicians', icon: 'pi pi-users', to: '/technicians' },
  { key: 'maps', label: 'Maps', icon: 'pi pi-map', to: '/maps' },
  { key: 'gps', label: 'GPS', icon: 'pi pi-compass', to: '/gps' },
  { key: 'daily_loadsheet', label: 'Daily Loadsheet', icon: 'pi pi-list', to: '/daily-loadsheet' },
  { key: 'equipment', label: 'Equipment', icon: 'pi pi-cog', to: '/equipment' },
  { key: 'customer_portal', label: 'Customer Portal', icon: 'pi pi-globe', to: '/customer-portal' },
  { key: 'appointments', label: 'Appointments', icon: 'pi pi-calendar', to: '/appointments' },
  { key: 'tasks', label: 'Tasks', icon: 'pi pi-check', to: '/tasks' },
  { key: 'checklists', label: 'Checklists', icon: 'pi pi-list', to: '/checklists' },
  { key: 'job_templates', label: 'Job Templates', icon: 'pi pi-clone', to: '/job-templates' },
  { key: 'customers', label: 'Customers', icon: 'pi pi-users', to: '/customers' },
  { key: 'estimates', label: 'Estimates', icon: 'pi pi-file-edit', to: '/estimates' },
  { key: 'billing', label: 'Billing', icon: 'pi pi-dollar', to: '/billing', permission: 'invoices.read_all' },
  // Duplicate payroll entries — must collapse to ONE.
  { key: 'payroll', label: 'Payroll', icon: 'pi pi-money-bill', to: '/payroll', permission: 'payroll.read' },
  { key: 'payroll', label: 'Payroll', icon: 'pi pi-wallet', to: '/admin/payroll', permission: 'payroll.read' },
  // Admin-only items that must never show to a tech.
  { key: 'webhooks', label: 'Webhooks', icon: 'pi pi-bell', to: '/webhooks' },
  { key: 'role_permissions', label: 'Roles & Permissions', icon: 'pi pi-lock', to: '/role-permissions' },
  { key: 'sso', label: 'SSO', icon: 'pi pi-key', to: '/sso' },
  { key: 'gdpr', label: 'GDPR & Compliance', icon: 'pi pi-shield', to: '/gdpr' },
];

// MH-9: the 15 keys moved from FIELD_TECH_MODULES → OFFICE_MODULES.
// Asserted dropped from tech, retained for office.
const MH9_DROPPED_FROM_TECH = [
  'dispatch', 'scheduling', 'appointments', 'tasks', 'planner',
  'checklists', 'job_templates', 'maintenance', 'technicians',
  'maps', 'gps', 'daily_loadsheet', 'equipment', 'parts_to_order',
  'customer_portal',
];

function _allKeys(sections) {
  return sections.flatMap((s) => s.modules.map((m) => m.key));
}

describe('useModuleSections — role gate', () => {
  it('tech sees only field-relevant modules; admin/finance items hidden', () => {
    const s = groupModulesForRole(CATALOG, 'tech');
    const keys = _allKeys(s);
    expect(keys).toContain('jobs');
    expect(keys).toContain('timeclock');
    expect(keys).toContain('photos');
    expect(keys).toContain('inventory');
    expect(keys).toContain('inbox');
    expect(keys).toContain('communications');
    // Money / admin items MUST be absent
    expect(keys).not.toContain('billing');
    expect(keys).not.toContain('payroll');
    expect(keys).not.toContain('webhooks');
    expect(keys).not.toContain('role_permissions');
    expect(keys).not.toContain('sso');
    expect(keys).not.toContain('gdpr');
  });

  it('MH-9b: tech sees Profile under Account section when injected', () => {
    const catalogPlusProfile = [
      ...CATALOG,
      { key: 'profile', label: 'Profile', icon: 'pi pi-user', to: '/profile' },
    ];
    const s = groupModulesForRole(catalogPlusProfile, 'tech');
    const sectionOf = (k) => s.find((b) => b.modules.some((m) => m.key === k))?.section;
    expect(sectionOf('profile')).toBe('Account');
  });

  it('MH-9: dispatcher/planner/scheduling/maps/parts-to-order etc. NOT visible to tech', () => {
    const s = groupModulesForRole(CATALOG, 'tech');
    const keys = _allKeys(s);
    for (const dropped of MH9_DROPPED_FROM_TECH) {
      expect(keys, `tech should NOT see "${dropped}" post-MH-9`).not.toContain(dropped);
    }
  });

  it('also treats backend canonical "technician" role as tech', () => {
    const s = groupModulesForRole(CATALOG, 'technician');
    const keys = _allKeys(s);
    expect(keys).not.toContain('webhooks');
    expect(keys).not.toContain('payroll');
    expect(keys).not.toContain('dispatch');
    expect(keys).not.toContain('parts_to_order');
  });

  it('office role adds customers + money basics but still hides admin items', () => {
    const s = groupModulesForRole(CATALOG, 'office');
    const keys = _allKeys(s);
    expect(keys).toContain('customers');
    expect(keys).toContain('estimates');
    expect(keys).toContain('billing');
    expect(keys).not.toContain('webhooks');
    expect(keys).not.toContain('role_permissions');
    expect(keys).not.toContain('sso');
    expect(keys).not.toContain('gdpr');
  });

  it('MH-9: office role retains the 15 keys that techs lost', () => {
    const s = groupModulesForRole(CATALOG, 'office');
    const keys = _allKeys(s);
    for (const moved of MH9_DROPPED_FROM_TECH) {
      expect(keys, `office should still see "${moved}" post-MH-9`).toContain(moved);
    }
  });

  it('admin/owner/super_admin see the full catalog', () => {
    for (const role of ['admin', 'owner', 'super_admin']) {
      const s = groupModulesForRole(CATALOG, role);
      const keys = _allKeys(s);
      expect(keys).toContain('webhooks');
      expect(keys).toContain('sso');
      expect(keys).toContain('gdpr');
      expect(keys).toContain('payroll');
    }
  });
});

describe('useModuleSections — grouping', () => {
  it('emits sections in the documented order', () => {
    const s = groupModulesForRole(CATALOG, 'admin');
    const names = s.map((b) => b.section);
    const positions = names.map((n) => SECTION_ORDER.indexOf(n));
    // Each section's index in the output must be ascending in SECTION_ORDER
    for (let i = 1; i < positions.length; i++) {
      if (positions[i - 1] === -1 || positions[i] === -1) continue;
      expect(positions[i]).toBeGreaterThan(positions[i - 1]);
    }
  });

  it('drops empty sections', () => {
    // tech list won't have a "Money" section since billing/payroll/etc.
    // are filtered out by the allowlist
    const s = groupModulesForRole(CATALOG, 'tech');
    const names = s.map((b) => b.section);
    expect(names).not.toContain('Money');
    expect(names).not.toContain('Admin');
  });

  it('routes a known key to its documented section', () => {
    const s = groupModulesForRole(CATALOG, 'admin');
    const sectionOf = (k) => s.find((b) => b.modules.some((m) => m.key === k))?.section;
    expect(sectionOf('jobs')).toBe('Field');
    expect(sectionOf('inbox')).toBe('Customers & Comms');
    expect(sectionOf('billing')).toBe('Money');
    expect(sectionOf('inventory')).toBe('Inventory');
    expect(sectionOf('webhooks')).toBe('Admin');
  });

  it('lands an unknown key in "Other" rather than dropping it', () => {
    const s = groupModulesForRole(
      [...CATALOG, { key: 'experimental_thing', label: 'Experimental', icon: '', to: '/x' }],
      'admin',
    );
    const sectionOf = (k) => s.find((b) => b.modules.some((m) => m.key === k))?.section;
    expect(sectionOf('experimental_thing')).toBe('Other');
  });
});

describe('useModuleSections — search', () => {
  it('case-insensitive label match', () => {
    const s = groupModulesForRole(CATALOG, 'admin', 'PAYR');
    const keys = _allKeys(s);
    expect(keys).toEqual(['payroll']);
  });

  it('matches by route too', () => {
    const s = groupModulesForRole(CATALOG, 'admin', '/webhooks');
    const keys = _allKeys(s);
    expect(keys).toEqual(['webhooks']);
  });

  it('returns an empty section list when no match', () => {
    const s = groupModulesForRole(CATALOG, 'admin', 'zzznotamodule');
    expect(s).toEqual([]);
  });

  it('empty / whitespace query is treated as no filter', () => {
    const a = groupModulesForRole(CATALOG, 'admin', '');
    const b = groupModulesForRole(CATALOG, 'admin', '   ');
    expect(_allKeys(a).length).toBeGreaterThan(0);
    expect(_allKeys(b).length).toBe(_allKeys(a).length);
  });
});

describe('useModuleSections — Payroll de-duplication', () => {
  it('collapses both Payroll catalog entries into one (label+permission de-dupe)', () => {
    const s = groupModulesForRole(CATALOG, 'admin');
    const payrolls = s.flatMap((b) => b.modules).filter((m) => m.label === 'Payroll');
    expect(payrolls.length).toBe(1);
  });

  it('keeps the FIRST Payroll entry (canonical `/payroll`, not the admin alias)', () => {
    const s = groupModulesForRole(CATALOG, 'admin');
    const payroll = s.flatMap((b) => b.modules).find((m) => m.label === 'Payroll');
    expect(payroll?.to).toBe('/payroll');
  });
});

describe('isModuleAllowedForRole — DT-1 desktop sidebar + CommandPalette gate', () => {
  // The new exported helper is what AppSidebar and CommandPalette use to
  // gate their non-section surfaces (icon strip, top pins, Ctrl-K search).
  // Lock its behaviour matches `groupModulesForRole`'s internal gate so the
  // two never drift.

  it('tech: allows every FIELD_TECH_MODULES key', () => {
    for (const key of FIELD_TECH_MODULES) {
      expect(isModuleAllowedForRole(key, 'tech')).toBe(true);
    }
  });

  it('tech: blocks every OFFICE_MODULES key (the 15 MH-9 dropped)', () => {
    for (const key of OFFICE_MODULES) {
      expect(isModuleAllowedForRole(key, 'tech')).toBe(false);
    }
  });

  it('tech: blocks admin-only items (webhooks, sso, payroll, role_permissions)', () => {
    for (const key of ['webhooks', 'sso', 'payroll', 'role_permissions', 'gdpr']) {
      expect(isModuleAllowedForRole(key, 'tech')).toBe(false);
    }
  });

  it('technician (long form) resolves identically to tech', () => {
    for (const key of OFFICE_MODULES) {
      expect(isModuleAllowedForRole(key, 'technician')).toBe(false);
    }
    expect(isModuleAllowedForRole('jobs', 'technician')).toBe(true);
  });

  it('dispatcher / sales / office: allows FIELD_TECH + OFFICE keys, blocks admin-only', () => {
    for (const role of ['dispatcher', 'sales', 'office']) {
      expect(isModuleAllowedForRole('jobs', role)).toBe(true);
      expect(isModuleAllowedForRole('dispatch', role)).toBe(true);
      expect(isModuleAllowedForRole('customers', role)).toBe(true);
      expect(isModuleAllowedForRole('webhooks', role)).toBe(false);
      expect(isModuleAllowedForRole('sso', role)).toBe(false);
    }
  });

  it('admin / owner / super_admin: allows everything', () => {
    for (const role of ['admin', 'owner', 'super_admin']) {
      expect(isModuleAllowedForRole('webhooks', role)).toBe(true);
      expect(isModuleAllowedForRole('payroll', role)).toBe(true);
      expect(isModuleAllowedForRole('any_future_key', role)).toBe(true);
    }
  });

  it('null / undefined / empty role: falls through to office set (defensive)', () => {
    // Pre-login or pre-role-resolved state — treat as office, NOT admin.
    expect(isModuleAllowedForRole('jobs', null)).toBe(true);
    expect(isModuleAllowedForRole('webhooks', null)).toBe(false);
    expect(isModuleAllowedForRole('jobs', undefined)).toBe(true);
    expect(isModuleAllowedForRole('webhooks', '')).toBe(false);
  });
});

describe('useModuleSections — invariants', () => {
  it('FIELD_TECH_MODULES and OFFICE_MODULES are disjoint by design (additive policy)', () => {
    const overlap = [...FIELD_TECH_MODULES].filter((k) => OFFICE_MODULES.has(k));
    expect(overlap).toEqual([]);
  });

  it('SECTION_ORDER includes the 6 documented sections + "Other" fallback', () => {
    expect(SECTION_ORDER).toEqual([
      'Field', 'Customers & Comms', 'Money', 'Inventory', 'Admin', 'Account', 'Other',
    ]);
  });
});
