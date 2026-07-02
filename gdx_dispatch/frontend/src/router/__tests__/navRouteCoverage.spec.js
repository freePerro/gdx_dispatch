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
 */
import { describe, it, expect } from 'vitest';
import { routes } from '../index';
import { MODULE_CATEGORIES, flattenModules } from '../../constants/modules';

// Build the set of matchable static paths. Param routes (:id) are matched
// by prefix when a nav path can't hit them directly (nav never points at
// param routes today, so static matching is sufficient and strict).
const staticPaths = new Set(
  routes.filter((r) => !r.path.includes(':')).map((r) => r.path)
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
    const broken = routes
      .filter((r) => typeof r.redirect === 'string')
      .filter((r) => !routeExists(r.redirect))
      .map((r) => `${r.path} → ${r.redirect}`);
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
