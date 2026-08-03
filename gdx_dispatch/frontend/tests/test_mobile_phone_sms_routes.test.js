// Mobile Phone.com companions (2026-08-03): voicemail/calls + SMS views for
// the above-technician tier. Guards: routes registered + nav.office-gated,
// and the MH-5 companion map sends phone-viewport users off the desktop
// tables onto the companions.
import { describe, expect, it } from 'vitest';
import { createAppRouter } from '../src/router';

describe('mobile phone/sms routes', () => {
  it('registers /mobile/phone gated on nav.office', () => {
    const router = createAppRouter();
    const match = router.resolve('/mobile/phone');
    expect(match.matched.length).toBeGreaterThan(0);
    expect(match.name).toBe('mobile-phone');
    expect(match.meta.requiresPermission).toBe('nav.office');
    expect(match.meta.noSidebar).toBe(true);
  });

  it('registers /mobile/sms gated on nav.office', () => {
    const router = createAppRouter();
    const match = router.resolve('/mobile/sms');
    expect(match.matched.length).toBeGreaterThan(0);
    expect(match.name).toBe('mobile-sms');
    expect(match.meta.requiresPermission).toBe('nav.office');
    expect(match.meta.noSidebar).toBe(true);
  });
});

describe('phone-com mobile companions (MH-5 map)', () => {
  it('maps the desktop phone-com routes to the mobile companions when the mobile preference is forced', async () => {
    const { useViewMode } = await import('../src/composables/useViewMode');
    const vm = useViewMode();
    // 'mobile' preference makes companion mapping viewport-independent —
    // the jsdom test viewport is desktop-shaped.
    vm.forceMobile();
    try {
      expect(vm.mobileCompanionFor('/phone-com/calls')).toBe('/mobile/phone');
      expect(vm.mobileCompanionFor('/phone-com/messages')).toBe('/mobile/sms');
      // Cold leads / faxes stay desktop-only — no accidental mapping.
      expect(vm.mobileCompanionFor('/phone-com/cold-leads')).toBeNull();
    } finally {
      vm.resetPreference();
    }
  });
});
