/**
 * pluginMobileFriendly — which plugins get the More drawer's "Desktop" pill.
 *
 * Before this, the drawer decided phone-friendliness by looking a module's
 * path up in a set of literal paths (AppBottomNav MOBILE_FRIENDLY_PATHS). A
 * plugin's path is `/plugins/<key>` — only known at runtime — so no plugin
 * could ever match, and every plugin was flagged "Desktop" no matter what it
 * rendered. The decision now comes from the plugin's own UI manifest.
 *
 * The rule that matters (2026-08-11 audit): NOT "has no browser screen". CHI
 * pricing — the plugin that is actually live in production — has a browser
 * capture tab AND three tabs (Captured Quotes / Settings / Help) that are
 * exactly what someone wants on a phone. Writing the whole plugin off because
 * of one desktop-only tab made the pill a no-op for every plugin in prod.
 */
import { describe, it, expect } from 'vitest';
import {
  pluginMobileFriendly,
  pluginNavCategory,
  placePluginModules,
  sanitizePluginIcon,
} from '../useTenantModules';

const CHI = {
  key: 'chipricing',
  ui: {
    screens: [
      { type: 'browser', title: 'HubX Workspace' },
      { type: 'list', title: 'Captured Quotes' },
      { type: 'settings', title: 'Settings' },
      { type: 'help', title: 'Help' },
    ],
  },
};

const MIDLAND = {
  key: 'midland',
  ui: {
    screens: [
      { type: 'list', title: 'Quote Builder' },
      { type: 'list', title: 'Order from Opening' },
      { type: 'list', title: 'Multipliers' },
      { type: 'help', title: 'Help' },
    ],
  },
};

describe('pluginMobileFriendly', () => {
  it('is true for a plugin whose screens are all phone-shaped', () => {
    expect(pluginMobileFriendly(MIDLAND)).toBe(true);
  });

  it('is true when only SOME screens need a desktop (the live-CHI case)', () => {
    // The regression this pins: a `browser` tab must not condemn the list,
    // settings and help tabs a tech actually wants in the field.
    expect(pluginMobileFriendly(CHI)).toBe(true);
  });

  it('is false when every screen is a streamed browser', () => {
    expect(pluginMobileFriendly({ ui: { screens: [{ type: 'browser' }] } })).toBe(false);
  });

  it('is false for a plugin that declares no usable UI', () => {
    expect(pluginMobileFriendly({ ui: { screens: [] } })).toBe(false);
    expect(pluginMobileFriendly({ ui: {} })).toBe(false);
    expect(pluginMobileFriendly({})).toBe(false);
    expect(pluginMobileFriendly(null)).toBe(false);
    expect(pluginMobileFriendly(undefined)).toBe(false);
  });

  it('does not crash on a malformed screens payload', () => {
    expect(pluginMobileFriendly({ ui: { screens: 'nope' } })).toBe(false);
    expect(pluginMobileFriendly({ ui: { screens: [null] } })).toBe(true); // untyped ≠ browser
  });
});

describe('sanitizePluginIcon', () => {
  it('passes a single PrimeIcons pair through', () => {
    expect(sanitizePluginIcon('pi pi-history')).toBe('pi pi-history');
    expect(sanitizePluginIcon('pi pi-chart-line')).toBe('pi pi-chart-line');
  });

  it('falls back to the box for anything else — the icon lands on <i :class>', () => {
    // The manifest validates server-side too; this pins the frontend's
    // never-trust-the-wire guard against class injection from the catalog.
    expect(sanitizePluginIcon('pi pi-box evil-class')).toBe('pi pi-box');
    expect(sanitizePluginIcon('fa fa-bomb')).toBe('pi pi-box');
    expect(sanitizePluginIcon('pi pi-UPPER')).toBe('pi pi-box');
    expect(sanitizePluginIcon('')).toBe('pi pi-box');
    expect(sanitizePluginIcon(null)).toBe('pi pi-box');
    expect(sanitizePluginIcon(undefined)).toBe('pi pi-box');
    expect(sanitizePluginIcon(7)).toBe('pi pi-box');
  });
});

describe('pluginNavCategory', () => {
  it('accepts a real MODULE_CATEGORIES key', () => {
    expect(pluginNavCategory({ ui: { category: 'operations' } })).toBe('operations');
    expect(pluginNavCategory({ ui: { category: 'invoicing' } })).toBe('invoicing');
  });

  it('degrades unknown or malformed values to null (Plugins group)', () => {
    // A typo'd manifest must still show up somewhere, not vanish from the nav.
    expect(pluginNavCategory({ ui: { category: 'moneystuff' } })).toBe(null);
    expect(pluginNavCategory({ ui: { category: 'Operations' } })).toBe(null);
    expect(pluginNavCategory({ ui: { category: 42 } })).toBe(null);
    expect(pluginNavCategory({ ui: {} })).toBe(null);
    expect(pluginNavCategory({})).toBe(null);
    expect(pluginNavCategory(null)).toBe(null);
  });

  it('refuses the reserved admin/experimental groups', () => {
    // Admin implies host-level authority a third-party entry must not borrow
    // (a single-entry category flattens to a bare top-level link, so a plugin
    // could pose AS the Admin item); Experimental is the core feature-flag
    // shelf. Both are real MODULE_CATEGORIES keys — the deny-list is the only
    // thing keeping them out.
    expect(pluginNavCategory({ ui: { category: 'admin' } })).toBe(null);
    expect(pluginNavCategory({ ui: { category: 'experimental' } })).toBe(null);
  });
});

describe('placePluginModules', () => {
  const entry = (key, category = null) => ({
    key: `plugin:${key}`,
    label: key,
    icon: 'pi pi-box',
    to: `/plugins/${key}`,
    type: 'Plugin',
    category,
    mobile_friendly: true,
  });
  const baseOps = { key: 'operations', label: 'Operations', icon: 'pi pi-cog', modules: [{ key: 'jobs' }] };
  const baseSales = { key: 'sales', label: 'Sales', icon: 'pi pi-chart-line', modules: [{ key: 'leads' }] };

  it('keeps uncategorized plugins in a trailing Plugins group (old behavior)', () => {
    const out = placePluginModules([baseOps], [entry('eventlog')]);
    expect(out.map((c) => c.key)).toEqual(['operations', 'plugins']);
    expect(out[1].modules.map((m) => m.key)).toEqual(['plugin:eventlog']);
    expect(out[0].modules).toEqual(baseOps.modules);
  });

  it('appends a categorized plugin after the category core modules', () => {
    const out = placePluginModules([baseOps, baseSales], [entry('quoter', 'sales')]);
    expect(out.map((c) => c.key)).toEqual(['operations', 'sales']);
    expect(out[1].modules.map((m) => m.key)).toEqual(['leads', 'plugin:quoter']);
    // No empty Plugins group when every plugin placed itself elsewhere.
  });

  it('recreates a hidden category at its canonical position for a plugin targeting it', () => {
    // 'operations' precedes 'sales' in MODULE_CATEGORIES; base lacks it
    // (all core modules disabled) but the plugin still needs a home there.
    const out = placePluginModules([baseSales], [entry('fieldtool', 'operations')]);
    expect(out.map((c) => c.key)).toEqual(['operations', 'sales']);
    expect(out[0].label).toBe('Operations');
    expect(out[0].modules.map((m) => m.key)).toEqual(['plugin:fieldtool']);
  });

  it('routes mixed entries and never mutates the base categories', () => {
    const out = placePluginModules(
      [baseOps],
      [entry('a', 'operations'), entry('b'), entry('manage')],
    );
    expect(out.map((c) => c.key)).toEqual(['operations', 'plugins']);
    expect(out[0].modules.map((m) => m.key)).toEqual(['jobs', 'plugin:a']);
    expect(out[1].modules.map((m) => m.key)).toEqual(['plugin:b', 'plugin:manage']);
    expect(baseOps.modules.map((m) => m.key)).toEqual(['jobs']); // untouched
  });

  it('returns base unchanged when there are no plugins at all', () => {
    expect(placePluginModules([baseOps], [])).toEqual([baseOps]);
  });
});
