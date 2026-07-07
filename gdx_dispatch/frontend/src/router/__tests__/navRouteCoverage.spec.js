/**
 * Nav ↔ route coverage guard — 2026-07-01 UX audit.
 *
 * Two real bugs motivated this spec:
 *   - /voice redirected to /phone-com, which was never a route → legacy
 *     bookmarks landed on the 404 catch-all.
 *   - Two nav entries shared the module key 'payroll' → one enablement
 *     key silently controlled both, and key-keyed maps collided.
 *
 * Pins:
 *   1. Every module entry's `to` resolves to a registered route (no
 *      orphan nav items pointing into the 404).
 *   2. Every redirect target resolves to a registered route.
 *   3. Module keys are globally unique.
 *
 * 2026-07-07 tabbed-pages: cluster routes nest under ModuleTabsPage
 * parents, so path collection now walks `children` (absolute child paths
 * allowed, '' = index at the parent's path). New pins guard the cluster
 * wiring: every module `cluster` tag resolves to a NAV_CLUSTERS def, each
 * cluster has ≥2 children living in ONE category (the collapse pass works
 * on runs within a category), and cluster keys don't collide with module
 * keys (both land in key-keyed maps).
 */
import { describe, it, expect } from 'vitest';
import { routes } from '../index';
import { MODULE_CATEGORIES, NAV_CLUSTERS, flattenModules } from '../../constants/modules';

// Flatten nested route records into (record, fullPath) pairs. Child paths
// starting with '/' are absolute (Vue Router semantics); '' is the index
// route at the parent's own path.
function collectRoutes(list, base = '') {
  const out = [];
  for (const record of list) {
    let fullPath;
    if (record.path === '') fullPath = base || '/';
    else if (record.path.startsWith('/')) fullPath = record.path;
    else fullPath = `${base.replace(/\/$/, '')}/${record.path}`;
    out.push({ record, fullPath });
    if (record.children) out.push(...collectRoutes(record.children, fullPath));
  }
  return out;
}

const allRoutes = collectRoutes(routes);

// Build the set of matchable static paths. Param routes (:id) are matched
// by prefix when a nav path can't hit them directly (nav never points at
// param routes today, so static matching is sufficient and strict).
const staticPaths = new Set(
  allRoutes.filter(({ fullPath }) => !fullPath.includes(':')).map(({ fullPath }) => fullPath)
);

function routeExists(path) {
  return staticPaths.has(path);
}

describe('nav ↔ route coverage', () => {
  it('every nav module points at a registered route', () => {
    const missing = flattenModules()
      .filter((m) => m.to && !routeExists(m.to))
      .map((m) => `${m.key} → ${m.to}`);
    expect(missing).toEqual([]);
  });

  it('every redirect lands on a registered route (no bookmark 404s)', () => {
    const broken = allRoutes
      .filter(({ record }) => typeof record.redirect === 'string')
      .filter(({ record }) => !routeExists(record.redirect))
      .map(({ record, fullPath }) => `${fullPath} → ${record.redirect}`);
    expect(broken).toEqual([]);
  });

  it('module keys are globally unique across categories', () => {
    const seen = new Map();
    const dupes = [];
    for (const cat of MODULE_CATEGORIES) {
      for (const m of cat.modules) {
        if (seen.has(m.key)) dupes.push(`${m.key} (${seen.get(m.key)} + ${cat.key})`);
        seen.set(m.key, cat.key);
      }
    }
    expect(dupes).toEqual([]);
  });

  it('the games entry is surfaced in Experimental with the future-release blurb', () => {
    const experimental = MODULE_CATEGORIES.find((c) => c.key === 'experimental');
    const games = experimental?.modules.find((m) => m.key === 'games');
    expect(games).toBeTruthy();
    expect(games.to).toBe('/admin/games');
    expect(games.label).toMatch(/future release/i);
    expect(games.description).toMatch(/game theory for motivation/i);
  });
});

describe('nav clusters (tabbed pages)', () => {
  const clusterKeys = new Set(NAV_CLUSTERS.map((c) => c.key));

  it('every module cluster tag resolves to a NAV_CLUSTERS definition', () => {
    const orphanTags = flattenModules()
      .filter((m) => m.cluster && !clusterKeys.has(m.cluster))
      .map((m) => `${m.key} → ${m.cluster}`);
    expect(orphanTags).toEqual([]);
  });

  it('every cluster has at least two children, all in one category', () => {
    const problems = [];
    for (const cluster of NAV_CLUSTERS) {
      const homes = MODULE_CATEGORIES.filter((cat) =>
        cat.modules.some((m) => m.cluster === cluster.key)
      );
      const children = flattenModules().filter((m) => m.cluster === cluster.key);
      if (children.length < 2) problems.push(`${cluster.key}: ${children.length} child(ren)`);
      if (homes.length !== 1) {
        problems.push(`${cluster.key}: spans ${homes.map((c) => c.key).join('+') || 'no'} categories`);
      }
    }
    expect(problems).toEqual([]);
  });

  it('cluster keys do not collide with module keys', () => {
    const moduleKeys = new Set(flattenModules().map((m) => m.key));
    const collisions = NAV_CLUSTERS.filter((c) => moduleKeys.has(c.key)).map((c) => c.key);
    expect(collisions).toEqual([]);
  });

  it('cluster children keep their tab caption', () => {
    const missing = flattenModules()
      .filter((m) => m.cluster && !m.tabLabel)
      .map((m) => m.key);
    expect(missing).toEqual([]);
  });
});
