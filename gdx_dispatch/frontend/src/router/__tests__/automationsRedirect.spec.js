/**
 * 2026-08-31: the "Automations" sequences page was retired — it created
 * sequences nothing ever executed. The path must keep working for old links
 * and bookmarks by landing on Event Rules (the engine that runs), and no
 * route may quietly bring the dead view back.
 */
import { describe, expect, it } from 'vitest';
import { routes } from '../index.js';
import { MODULE_CATEGORIES, NAV_CLUSTERS, flattenModules } from '../../constants/modules.js';

function flatten(list) {
  return list.flatMap((r) => [r, ...(r.children ? flatten(r.children) : [])]);
}

describe('/automations after the sequences page was retired', () => {
  it('redirects to the Event Rules page instead of rendering a component', () => {
    const hit = flatten(routes).filter((r) => r.path === '/automations');
    expect(hit).toHaveLength(1);
    expect(hit[0].redirect).toBe('/automation-rules');
    expect(hit[0].component).toBeUndefined();
  });

  it('still has a real Event Rules route to land on', () => {
    const rules = flatten(routes).find((r) => r.path === '/automation-rules');
    expect(rules).toBeTruthy();
    expect(rules.component).toBeTruthy();
  });

  it('has no nav entry pointing at the dead page', () => {
    const items = flattenModules(MODULE_CATEGORIES);
    expect(items.some((m) => m.to === '/automations')).toBe(false);
    const rulesNav = items.find((m) => m.to === '/automation-rules');
    expect(rulesNav).toBeTruthy();
    // The module toggle that gates /api/workflows must gate its nav entry too.
    expect(rulesNav.requires).toBe('automations');
    const hub = NAV_CLUSTERS.find((c) => c.key === 'marketing_hub');
    expect(hub && hub.description).not.toMatch(/automations/i);
  });
});
