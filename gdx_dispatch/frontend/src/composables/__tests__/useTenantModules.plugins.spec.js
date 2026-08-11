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
import { pluginMobileFriendly } from '../useTenantModules';

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
