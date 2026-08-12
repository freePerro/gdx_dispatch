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
    // Intentionally not mapped: /mobile/jobs defaults to "My jobs", so an
    // office user would land on a near-empty list (the older reason given here
    // — that it is tech-ONLY — is out of date; it has a company scope toggle).
    expect(vm.mobileCompanionFor('/jobs')).toBeNull();
    // /profile fits at 390px via a responsive clamp, no companion needed.
    expect(vm.mobileCompanionFor('/profile')).toBeNull();
    // /reports has no phone-shaped equivalent to send anyone to.
    expect(vm.mobileCompanionFor('/reports')).toBeNull();
    expect(vm.mobileCompanionFor('/dashboard')).toBeNull();
    expect(vm.mobileCompanionFor('/billing')).toBeNull();
  });

  it('returns null when on auto preference and viewport reports desktop', () => {
    // jsdom defaults isMobileViewport to false (no matchMedia match);
    // the auto path requires isMobileViewport.value === true to fire.
    vm.resetPreference();
    // In a real desktop browser auto + non-mobile viewport → no redirect.
    expect(vm.mobileCompanionFor('/customers')).toBeNull();
  });
});

/**
 * Why /billing, /estimates and /inventory are NOT companion-mapped.
 *
 * The 2026-08-12 phone audit measured those desktop tables at 885/735/595px on
 * a 390px screen, and all three have mobile companions that measured clean — so
 * mapping them looks obviously right. It was staged and then reverted in review,
 * because a redirect trades a width problem for three worse ones:
 *
 *   1. it is PERMANENT. `preference === 'desktop'` is the only escape and
 *      forceDesktop() is called from nowhere in the UI.
 *   2. the companions are narrower, not equivalent — MobileInventoryView is
 *      read-only, convert-to-job exists in no Mobile* view, and
 *      MobileBillingView has none of the Ready-for-Billing dismissal verbs.
 *   3. three call sites deep-link into /billing with a query the companion
 *      never reads, so "Create Invoice" on a job would land on a plain list and
 *      silently do nothing.
 *
 * These assertions exist so re-adding the mapping is a deliberate act with a
 * failing test attached, not a tidy-looking one-liner.
 */
describe('useViewMode — routes deliberately left unmapped', () => {
  let vm;
  beforeEach(() => {
    vm = useViewMode();
    vm.resetPreference();
  });

  it.each(['/billing', '/estimates', '/inventory', '/jobs', '/reports'])(
    'does not redirect %s on a phone', (path) => {
      vm.forceMobile();
      expect(vm.mobileCompanionFor(path)).toBeNull();
    },
  );

  it('still redirects the mappings that ARE safe', () => {
    vm.forceMobile();
    expect(vm.mobileCompanionFor('/customers')).toBe('/mobile/customers');
    expect(vm.mobileCompanionFor('/door-listings')).toBe('/mobile/door-listings');
  });
});
