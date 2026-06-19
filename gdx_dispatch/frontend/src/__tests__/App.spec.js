/**
 * App.vue — regression contract for the AppLayout-into-App.vue refactor.
 *
 * Before the refactor, every view wrapped itself in <AppLayout>. Vue could
 * leave a previous view's section orphaned in <main> when two consecutive
 * views shared the same template root (caught 2026-05-09 via /maps where
 * Google Maps mutated DOM Vue couldn't reconcile). The fix mounts
 * AppLayout exactly once here at the App root, with <router-view> as the
 * slot — so AppLayout never unmounts on navigation.
 *
 * These tests pin three properties that must hold for the bug not to
 * regress:
 *   1. App.vue imports AppLayout (it owns the mount).
 *   2. App.vue does NOT carry the :key="$route.fullPath" workaround
 *      that S110 added on <router-view> — the structural fix obsoletes
 *      that escape hatch.
 *   3. Routes can opt out via meta.noShell (login, signup, customer
 *      portal, the 404 fallback).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const APP_VUE = readFileSync(join(__dirname, '..', 'App.vue'), 'utf8');
const ROUTER = readFileSync(join(__dirname, '..', 'router', 'index.js'), 'utf8');

describe('App.vue — AppLayout mount contract', () => {
  it('imports AppLayout (the shell is mounted at App, not per-view)', () => {
    expect(APP_VUE).toMatch(/import\s+AppLayout\s+from\s+['"][^'"]*AppLayout\.vue['"]/);
  });

  it('renders <AppLayout v-if="!noShell"> wrapping <router-view>', () => {
    // The wrapping form: AppLayout takes router-view as its slot. Allow
    // whitespace flexibility; the structural assertion is "AppLayout
    // contains router-view inside its template body".
    expect(APP_VUE).toMatch(/<AppLayout\s+v-if="!noShell">[\s\S]*<router-view\s*\/>[\s\S]*<\/AppLayout>/);
  });

  it('has a bare <router-view v-else /> fallback for noShell routes', () => {
    expect(APP_VUE).toMatch(/<router-view\s+v-else\s*\/>/);
  });

  it('does NOT carry the :key="$route.fullPath" workaround (obsoleted by refactor)', () => {
    expect(APP_VUE).not.toMatch(/<router-view[^>]*:key=["']\$route\.fullPath["']/);
  });

  it('exposes a `noShell` computed driven by route meta', () => {
    expect(APP_VUE).toMatch(/route\?\.meta\?\.noShell/);
  });
});

describe('router — noShell meta on bare-shell routes', () => {
  const expectedNoShell = [
    '/login',
    '/login-picker',
    '/forgot-password',
    '/reset-password',
    '/signup',
    '/onboarding',
    '/customer-portal',
  ];

  it.each(expectedNoShell)('route %s carries noShell: true', (path) => {
    // Match a route declaration where path is `path` and the same record's
    // meta object includes noShell: true. Allow attributes to appear in
    // any order; rely on the simple-record shape used in routes[].
    const re = new RegExp(
      `path:\\s*['"]${path.replace(/\//g, '\\/')}['"][^}]*meta:\\s*\\{[^}]*noShell:\\s*true`,
    );
    expect(ROUTER).toMatch(re);
  });

  it('the 404 catch-all carries noShell: true', () => {
    expect(ROUTER).toMatch(/pathMatch[\s\S]{0,200}noShell:\s*true/);
  });
});
