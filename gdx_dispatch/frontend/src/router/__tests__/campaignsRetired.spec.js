/**
 * 2026-09-14 (#638): the Campaigns tab was retired with routers/campaigns.py,
 * whose send route reported a send it never made. Old links and bookmarks to
 * /campaigns and /marketing must still land somewhere real (Segments, the
 * cluster's first remaining tab), and no route or nav entry may bring the dead
 * view back.
 */
import { describe, expect, it } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';
import { routes } from '../index.js';
import { MODULE_CATEGORIES, NAV_CLUSTERS, flattenModules } from '../../constants/modules.js';

function flatten(list) {
  return list.flatMap((r) => [r, ...(r.children ? flatten(r.children) : [])]);
}

// Resolve a path through the real route table, redirects included, without
// the app's auth guards (they are not what is under test here).
async function landingPath(path) {
  const stub = { render: () => null };
  const stubbed = (list) => list.map((r) => ({
    ...r,
    ...(r.component ? { component: stub } : {}),
    ...(r.children ? { children: stubbed(r.children) } : {}),
  }));
  const router = createRouter({ history: createMemoryHistory(), routes: stubbed(routes) });
  await router.push(path);
  return router.currentRoute.value.path;
}

describe('the Campaigns tab after its retirement', () => {
  it('lands /campaigns on Segments', async () => {
    expect(await landingPath('/campaigns')).toBe('/segments');
  });

  it('lands the /marketing bookmark on Segments', async () => {
    expect(await landingPath('/marketing')).toBe('/segments');
  });

  it('routes no component for /campaigns, and Segments is a real page', () => {
    const all = flatten(routes);
    const campaigns = all.filter((r) => r.name === 'campaigns');
    expect(campaigns).toHaveLength(1);
    expect(campaigns[0].component).toBeUndefined();
    expect(campaigns[0].redirect).toBe('/segments');
    const segments = all.find((r) => r.path === '/segments');
    expect(segments && segments.component).toBeTruthy();
  });

  it('has no nav entry for campaigns, and the Marketing hub no longer promises them', () => {
    const items = flattenModules(MODULE_CATEGORIES);
    expect(items.some((m) => m.key === 'campaigns' || m.to === '/campaigns')).toBe(false);
    const hub = NAV_CLUSTERS.find((c) => c.key === 'marketing_hub');
    expect(hub).toBeTruthy();
    expect(hub.description).not.toMatch(/campaign/i);
    // The hub keeps its other tabs.
    const hubTabs = items.filter((m) => m.cluster === 'marketing_hub').map((m) => m.key);
    expect(hubTabs).toEqual(expect.arrayContaining(['segments', 'winback', 'loyalty']));
  });
});
