/**
 * The Invoicing entries gate on the `invoices` module — through the REAL
 * composable (GDXA-578).
 *
 * `billing`, `payments`, `collections` and `invoice_reminders` are catalog
 * keys, not backend modules. The composable gates on `requires || key` and
 * answers `true` for any key /api/settings/modules never names, so with
 * Invoices switched off all four stayed in the sidebar and every page 403'd
 * (routers/invoices.py, payments.py, collections.py and invoice_reminders.py
 * carry router-level require_module("invoices")). Same shape as the Inbox →
 * email fix in useTenantModules.emailGate.spec.js.
 *
 * The second block reads the backend registry itself: /api/settings/modules
 * returns MODULES keys only — never a LEGACY_MODULE_ALIASES key — so a
 * `requires` naming anything else can never fire.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }));

vi.mock('../useApi', () => ({
  useApi: () => ({ get: apiGet }),
  createApiClient: () => ({ get: apiGet }),
}));

// eslint-disable-next-line import/first
import { useTenantModules } from '../useTenantModules';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';
// eslint-disable-next-line import/first
import { MODULE_CATEGORIES } from '../../constants/modules';

const INVOICE_GATED = ['billing', 'payments', 'collections', 'invoice_reminders'];

function modulesPayload(entries) {
  apiGet.mockImplementation(async (url) => (url === '/api/settings/modules' ? { modules: entries } : []));
}

function visibleKeys(tm) {
  return new Set(tm.allEnabledModules.value.map((m) => m.key));
}

describe('useTenantModules — the Invoicing entries follow the invoices module', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    // The loader skips the fetch pre-auth; a token is enough to be "signed in".
    const auth = useAuthStore();
    auth.accessToken = 'test.access.token';
    // Hold every permission, so the module gate is the only filter in play.
    auth.permissions = new Set(['*']);
    apiGet.mockReset();
  });

  it('invoices switched off: the four invoice-backed entries leave the catalog', async () => {
    modulesPayload([{ key: 'invoices', enabled: false }]);
    const tm = useTenantModules();
    await tm.loadTenantModules({ force: true });
    const keys = visibleKeys(tm);
    for (const key of INVOICE_GATED) expect(keys.has(key), key).toBe(false);
    // Email Log reads /api/outbound-emails, which has no module gate.
    expect(keys.has('email_log')).toBe(true);
  });

  it('invoices on, or never stated: the entries are in the catalog', async () => {
    modulesPayload([{ key: 'invoices', enabled: true }]);
    const on = useTenantModules();
    await on.loadTenantModules({ force: true });
    for (const key of INVOICE_GATED) expect(visibleKeys(on).has(key), key).toBe(true);

    modulesPayload([]);
    const unstated = useTenantModules();
    await unstated.loadTenantModules({ force: true });
    for (const key of INVOICE_GATED) expect(visibleKeys(unstated).has(key), key).toBe(true);
  });
});

// The backend registry, read from source so the guard cannot drift from it.
function backendModuleKeys() {
  const src = readFileSync(resolve(__dirname, '../../../../core/modules.py'), 'utf8');
  const block = src.match(/^MODULES = \{\n([\s\S]*?)^\}/m);
  if (!block) throw new Error('MODULES block not found in core/modules.py');
  return new Set([...block[1].matchAll(/^ {4}"([a-z_]+)":/gm)].map((m) => m[1]));
}

// Catalog entries whose gate key (`requires || key`) is NOT a backend module,
// so no tenant toggle can hide them. Many are ungated by design (settings,
// users, Email Log); others sit on a module-gated router and are the same
// defect GDXA-578 fixed for Invoicing, deferred from it and counted in its
// report. This list may only shrink: a new entry must name a real module in
// `requires`, or be added here on purpose.
const UNGATED_ENTRIES = [
  'appointments', 'tasks', 'planner', 'checklists', 'maintenance', 'performance',
  'daily_loadsheet', 'photos', 'reviews', 'referrals', 'surveys', 'leads',
  'change_orders', 'service_agreements', 'signatures', 'email_log', 'expenses',
  'job_costing', 'labor_matrix', 'vendor_statements', 'vendor_bills', 'exports',
  'accounting_ledger', 'accounting_settings', 'payroll', 'commissions',
  'variance_report', 'forecasting', 'budget', 'spending_trends', 'overhead',
  'games', 'winback', 'catalog', 'door_listings', 'parts_to_order', 'vendors',
  'purchase_orders', 'pdf_templates', 'resources', 'users', 'role_permissions',
  'tags', 'custom_fields', 'webhooks', 'gdpr', 'activity', 'onboarding',
  'server_errors', 'audit_log', 'admin_db', 'admin_payroll', 'feedback_portal',
  'settings',
];

describe('MODULE_CATEGORIES gate keys against the backend registry', () => {
  const MODULES = backendModuleKeys();
  const entries = MODULE_CATEGORIES.flatMap((c) => c.modules);

  it('reads a plausible registry', () => {
    expect(MODULES.has('invoices')).toBe(true);
    expect(MODULES.has('email')).toBe(true);
    // An alias is not a registry key: /api/settings/modules never returns it.
    expect(MODULES.has('change_orders')).toBe(false);
  });

  it('every `requires` names a backend module', () => {
    const bad = entries.filter((m) => m.requires && !MODULES.has(m.requires)).map((m) => `${m.key}→${m.requires}`);
    expect(bad).toEqual([]);
  });

  it('every entry with no real gate key is on the ungated list, and the list carries no stale names', () => {
    const ungated = entries.filter((m) => !MODULES.has(m.requires || m.key)).map((m) => m.key);
    expect(ungated.filter((k) => !UNGATED_ENTRIES.includes(k))).toEqual([]);
    expect(UNGATED_ENTRIES.filter((k) => !ungated.includes(k))).toEqual([]);
  });
});
