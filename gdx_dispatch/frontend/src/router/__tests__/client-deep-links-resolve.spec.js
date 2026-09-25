/**
 * Every literal in-app path the SPA navigates to must exist in the route table.
 *
 * The Python-side guard (gdx_dispatch/tests/test_action_url_links_resolve.py)
 * reads server-produced deep links only, so it cannot see a client-side
 * `router.push('/somewhere')`. This is the client half of that net.
 *
 * It exists because of a live defect: BankFeedsView's SimpleFIN
 * "Re-link in Settings" button pushed '/settings/integrations', which is not a
 * route — only '/settings' and the deeper '/settings/integrations/outlook' are
 * registered — so the one control that repairs a stale bank feed landed the
 * user on NotFoundView. Same dead-'/settings/<child>' shape as the
 * '/settings/pricing' instance repointed earlier.
 *
 * Red-test check (what makes this guard able to fail): restore
 * `$router.push('/settings/integrations')` in BankFeedsView.vue and this spec
 * fails with that path named. A guard that cannot go red is not a guard.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineComponent, h } from 'vue';
import { mount } from '@vue/test-utils';
import { createRouter, createMemoryHistory, createWebHistory, RouterView } from 'vue-router';

import { routes } from '../index.js';

// src/router/__tests__ → src
const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

// Test fixtures deliberately navigate to made-up paths ('/elsewhere') to prove
// guard behaviour, so they are not part of the shipped navigation surface.
const SKIP_DIRS = new Set(['__tests__', 'node_modules', '__mocks__']);

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (!SKIP_DIRS.has(entry)) out.push(...walk(full));
    } else if (/\.(vue|js)$/.test(entry) && !/\.spec\.js$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

// `router.push('/x')`, `$router.push('/x')`, `router.push({ path: '/x' })`,
// and `<RouterLink to="/x">`. Template-literal and `:param` paths are dynamic
// and resolved at runtime, so they are out of scope here.
const PATTERNS = [
  /\brouter\.push\(\s*(['"])(\/[^'"]*)\1/g,
  /\brouter\.replace\(\s*(['"])(\/[^'"]*)\1/g,
  /\brouter\.(?:push|replace)\(\s*\{\s*path:\s*(['"])(\/[^'"]*)\1/g,
  /\bto="(\/[^"]*)"/g,
];

function literalPaths(source) {
  const found = [];
  for (const re of PATTERNS) {
    for (const m of source.matchAll(re)) {
      found.push(m[2] ?? m[1]);
    }
  }
  return found;
}

describe('client-side deep links resolve', () => {
  const router = createRouter({ history: createMemoryHistory(), routes });

  const collected = [];
  for (const file of walk(SRC)) {
    for (const path of literalPaths(readFileSync(file, 'utf8'))) {
      if (path.includes('${') || path.includes(':')) continue; // dynamic
      collected.push({ file: relative(SRC, file), path });
    }
  }

  it('finds navigation literals to check (guard is not vacuously green)', () => {
    expect(collected.length).toBeGreaterThan(20);
  });

  it('resolves every literal path to a real route, never NotFoundView', () => {
    const dead = [];
    for (const { file, path } of collected) {
      const resolved = router.resolve(path.split('?')[0]);
      if (resolved.name === 'not-found') dead.push(`${file} → ${path}`);
    }
    expect(dead).toEqual([]);
  });

  // The sidebar and bottom nav bind `:to="module.to"`, so those paths never
  // appear as a literal `to="/x"` and the scan above cannot see them. They are
  // the app's primary way in — a dead one is a dead nav item.
  //
  // Scoped to all of src, not just constants/modules.js: the same `to:` shape
  // lives in AppBottomNav, AppSidebar, useTenantModules and sidebarFavorites,
  // and an under-scoped guard would pass a dead bottom-nav target while
  // claiming to cover the class. `href="/x"` in a template is the same
  // navigation with a full page load, so it counts too.
  it('resolves every nav target and in-app href across src', () => {
    const NAV_PATTERNS = [
      /\bto:\s*['"](\/[^'"]*)['"]/g,
      /\bhref=['"](\/[^'"]*)['"]/g,
    ];
    const targets = [];
    for (const file of walk(SRC)) {
      const source = readFileSync(file, 'utf8');
      for (const re of NAV_PATTERNS) {
        for (const m of source.matchAll(re)) {
          targets.push({ file: relative(SRC, file), path: m[1] });
        }
      }
    }
    expect(targets.length).toBeGreaterThan(80);

    const dead = targets
      .filter(({ path }) => !path.includes('${') && !path.includes(':'))
      // /api/* is a server call, not a route; /docs and /pay/<token> are
      // server-rendered and deliberately absent from the SPA table.
      .filter(({ path }) => !path.startsWith('/api/') && !path.startsWith('/docs'))
      .filter(({ path }) => router.resolve(path.split('?')[0]).name === 'not-found')
      .map(({ file, path }) => `${file} → ${path}`);
    expect(dead).toEqual([]);
  });

  // The fourth spelling: `router.push({ name: 'x' })`. Same class — a
  // navigation target that is not in the route table.
  //
  // `hasRoute`, not `resolve`: most named targets here are param routes
  // ('job-detail' is `/jobs/:id`), and `resolve({ name })` with no params
  // THROWS "Missing required param" — indistinguishable, from the outside,
  // from an unknown name. Written that way first, this reported 'job-detail'
  // and 'GamePlayer' as dead when both are registered. The question being
  // asked is whether the name exists, and that is the method that asks it.
  it('resolves every literal route NAME the app navigates by', () => {
    const NAME_PATTERNS = [
      /\b(?:\$?router)\.(?:push|replace)\(\s*\{\s*name:\s*(['"])([^'"]+)\1/g,
      /\bto="\{\s*name:\s*'([^']+)'/g,
      /\b:to="\{\s*name:\s*'([^']+)'/g,
    ];
    const names = [];
    for (const file of walk(SRC)) {
      const source = readFileSync(file, 'utf8');
      for (const re of NAME_PATTERNS) {
        for (const m of source.matchAll(re)) {
          names.push({ file: relative(SRC, file), name: m[2] ?? m[1] });
        }
      }
    }
    // Not asserted as > N: name-based navigation is rare here by convention
    // (3 at the time of writing) and a future refactor to zero would be a
    // legitimate state, not a broken guard. The path scans above carry the
    // not-vacuous assertion.
    const dead = names
      .filter(({ name }) => !router.hasRoute(name))
      .map(({ file, name }) => `${file} → { name: '${name}' }`);
    expect(dead).toEqual([]);
  });

  // `redirect:` targets in the route table are the fifth spelling, and they
  // are NOT covered above: a redirect to a path that does not exist sends the
  // user to NotFoundView just as surely as a bad push, and nothing in src/
  // mentions the target for the scans to find. There are 10 of them.
  it('resolves every redirect target in the route table', () => {
    const seen = [];
    const collect = (list) => {
      for (const r of list) {
        if (typeof r.redirect === 'string') seen.push({ from: r.path, to: r.redirect });
        if (r.children) collect(r.children);
      }
    };
    collect(routes);
    expect(seen.length).toBeGreaterThan(5);
    const dead = seen
      .filter(({ to }) => router.resolve(to).name === 'not-found')
      .map(({ from, to }) => `${from} → ${to}`);
    expect(dead).toEqual([]);
  });

  // NOTE: the QUERY is deliberately not checked here — `router.resolve` is
  // handed `path.split('?')[0]`, so this file cannot fail for a dropped or
  // misspelled `?tab=`. That is not an oversight but it IS a hole, and it is
  // the exact hole the original defect lived in: the path and the query were
  // each tested and the link between them was not. Two files close it:
  //   * views/__tests__/SettingsTabDeepLink.spec.js — the receiving end, by
  //     mounting the real SettingsView. A source-text version of that check
  //     was written first and an audit broke the behaviour while leaving the
  //     text intact, so it stayed green;
  //   * views/__tests__/BankFeedsRelinkDestination.spec.js — across the seam,
  //     by pressing the real button and reading the resulting route. That is
  //     the only one of the three that fails if the button stops sending the
  //     tab at all.
  // Guarding the callers by regex was also tried and abandoned: it missed
  // three spellings of the same push, and no static scan can see a bookmark,
  // an emailed link, or a server-produced action_url anyway.
});

/**
 * SettingsView selects its tab from `window.location.search`, read ONCE during
 * setup(). The Bank Feeds re-link is a client-side `router.push`, so the whole
 * fix rests on vue-router having already written history before the incoming
 * component is created. If it had not, the user would land on Branding and the
 * flow would still be dead — silently, which is the #657 class.
 *
 * That claim was previously only exercised by an e2e that skips unless the
 * database happens to hold a broken SimpleFIN connection. It is a plain
 * ordering guarantee and belongs in the gate that always runs, so here it is:
 * jsdom implements history.pushState, and `createWebHistory` is what the app
 * builds (router/index.js).
 */
describe('a client-side push updates location.search before setup() reads it', () => {
  it('hands the query to the incoming component', async () => {
    let seenAtSetup = null;
    const Probe = defineComponent({
      setup() {
        // The exact read SettingsView performs.
        seenAtSetup = new URLSearchParams(window.location.search).get('tab');
        return () => h('div');
      },
    });

    const router = createRouter({
      history: createWebHistory(),
      routes: [
        { path: '/', component: { render: () => h('div') } },
        { path: '/settings', component: Probe },
      ],
    });

    const wrapper = mount(defineComponent({ render: () => h(RouterView) }), {
      global: { plugins: [router] },
    });
    await router.isReady();

    await router.push({ path: '/settings', query: { tab: 'integrations' } });
    await router.isReady();

    expect(seenAtSetup).toBe('integrations');
    expect(window.location.search).toBe('?tab=integrations');
    wrapper.unmount();
  });
});
