/**
 * 2026-09-01 (Phase D S2): the SaaS self-serve signup page and the
 * multi-tenant workspace picker were removed. Neither can come back quietly.
 *
 * What was there, and why it had to go:
 *
 *  - `/signup` was a PUBLIC route (`meta.public`) rendering a "Start your free
 *    trial / 14 days free" form that POSTed to `/signup` and expected a
 *    `checkout_url`. No such backend route exists — prod returned **405**. A
 *    stranger got a working-looking sign-up form for a product that is not
 *    sold, whose button failed.
 *
 *  - `PlatformRecovery` was a workspace picker redirecting to
 *    `https://<slug>.example.com/login` — the multi-tenant subdomain resolver
 *    Phase A deleted from the backend. It was reachable from a **"Wrong
 *    workspace?" button rendered unconditionally on the login form**, i.e. the
 *    front door, and confirmed live in the shipped prod bundle. Its
 *    "Don't have a workspace yet? Create one" link pointed at the dead
 *    `/signup`.
 *
 * See docs/design/phase-d-saas-residue.md (S2).
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { routes } from '../index.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.join(here, '..', '..');

function flatten(list) {
  return list.flatMap((r) => [r, ...(r.children ? flatten(r.children) : [])]);
}

describe('SaaS signup + workspace picker stay retired', () => {
  it('has no /signup route', () => {
    expect(flatten(routes).filter((r) => r.path === '/signup')).toHaveLength(0);
  });

  it('has no route referencing SignupView', () => {
    const offenders = flatten(routes)
      .map((r) => String(r.component ?? ''))
      .filter((c) => c.includes('SignupView'));
    expect(offenders).toEqual([]);
  });

  it('still has a catch-all, so /signup lands on Not Found rather than a blank', () => {
    // No dead ends: removing the route must not leave the path rendering nothing.
    const catchAll = flatten(routes).find((r) => r.path === '/:pathMatch(.*)*');
    expect(catchAll).toBeTruthy();
    expect(catchAll.name).toBe('not-found');
  });

  it('the deleted views and components are gone from the tree', () => {
    for (const rel of [
      'views/SignupView.vue',
      'components/PlatformRecovery.vue',
      'components/__tests__/PlatformRecovery.spec.js',
    ]) {
      expect(fs.existsSync(path.join(srcDir, rel))).toBe(false);
    }
  });

  it('the login page offers no workspace escape hatch', () => {
    // Absence assertion on the login source: a re-added picker, its test id, or
    // the multi-tenant subdomain it pointed at would all fail here.
    const login = fs.readFileSync(path.join(srcDir, 'views', 'LoginView.vue'), 'utf8');
    expect(login).not.toMatch(/wrong-workspace/);
    expect(login).not.toMatch(/PlatformRecovery/);
    expect(login).not.toMatch(/showRecovery/);
  });

  it('the login page carries no tenant-picker markup or styling, inline or extracted', () => {
    // Closes a gap an adversarial audit found in the assertions above: they
    // name the COMPONENT, so a picker re-added inline under different
    // identifiers would pass them all. LoginView really did carry the corpse of
    // an earlier inline picker — .tenant-picker/.picker-prompt/.tenant-choice/
    // .tenant-name/.tenant-slug — dead before this change and removed with it.
    // These are the identifiers a workspace picker needs whatever shape it takes.
    const login = fs.readFileSync(path.join(srcDir, 'views', 'LoginView.vue'), 'utf8');
    for (const marker of [
      'tenant-picker', 'picker-prompt', 'tenant-choice', 'tenant-slug',
    ]) {
      expect(login, `login page reintroduced "${marker}"`).not.toContain(marker);
    }
  });
});
