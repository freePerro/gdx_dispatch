/**
 * MH-5 — useViewMode mobile-companion redirect lock.
 *
 * Audit P1 #3 (systemic): non-tech users on a mobile viewport landed on
 * the desktop /customers table and hit horizontal overflow + clipped
 * search. The composable now exposes `mobileCompanionFor(path)` which
 * the router consults to redirect /customers → /mobile/customers when
 * the user is on a phone viewport.
 *
 * Tech-role redirects are handled in router/index.js (separate block);
 * this composable is the non-tech side of the policy.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { useViewMode } from '../useViewMode';

describe('useViewMode — mobileCompanionFor (MH-5)', () => {
  let vm;

  beforeEach(() => {
    // matchMedia in jsdom: control isMobileViewport via the module's
    // internal ref by setting the preference. The simplest path:
    // forceMobile() makes mobileCompanionFor return the mapping even
    // if the matchMedia query is non-mobile in jsdom. Tests that need
    // the desktop preference call forceDesktop().
    vm = useViewMode();
    vm.resetPreference();
  });

  it('returns /mobile/customers for /customers when on mobile (forced)', () => {
    vm.forceMobile();
    expect(vm.mobileCompanionFor('/customers')).toBe('/mobile/customers');
  });

  it('returns null on the desktop preference even if the path has a companion', () => {
    vm.forceDesktop();
    expect(vm.mobileCompanionFor('/customers')).toBeNull();
  });

  it('returns null for paths without a registered companion', () => {
    vm.forceMobile();
    // Intentionally not mapped — would be a regression (would hide
    // office/admin data behind tech-scoped /mobile/jobs).
    expect(vm.mobileCompanionFor('/jobs')).toBeNull();
    expect(vm.mobileCompanionFor('/profile')).toBeNull();
    expect(vm.mobileCompanionFor('/billing')).toBeNull();
    expect(vm.mobileCompanionFor('/dashboard')).toBeNull();
  });

  it('returns null when on auto preference and viewport reports desktop', () => {
    // jsdom defaults isMobileViewport to false (no matchMedia match);
    // the auto path requires isMobileViewport.value === true to fire.
    vm.resetPreference();
    // In a real desktop browser auto + non-mobile viewport → no redirect.
    expect(vm.mobileCompanionFor('/customers')).toBeNull();
  });
});
