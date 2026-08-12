import { ref } from 'vue';

const MOBILE_BREAKPOINT = '(max-width: 768px)';
const STORAGE_KEY = 'gdx.viewMode';

const preference = ref(loadPreference());
const isMobileViewport = ref(matchMobile());

if (typeof window !== 'undefined' && window.matchMedia) {
  const mql = window.matchMedia(MOBILE_BREAKPOINT);
  mql.addEventListener('change', (e) => { isMobileViewport.value = e.matches; });
}

function loadPreference() {
  try {
    return sessionStorage.getItem(STORAGE_KEY) || 'auto';
  } catch {
    return 'auto';
  }
}

function savePreference(value) {
  try {
    if (value === 'auto') sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, value);
  } catch { /* sessionStorage unavailable — preference is in-memory only */ }
}

function matchMobile() {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia(MOBILE_BREAKPOINT).matches;
}

export function useViewMode() {
  function setPreference(value) {
    preference.value = value;
    savePreference(value);
  }

  // Only auto-redirect on landing routes (post-login default destinations).
  // Redirecting on every nav makes Planner/Dispatch/More buttons no-op for
  // mobile-viewport users — the user clicked away on purpose, respect that.
  // 'mobile' preference still forces /mobile from anywhere as an explicit opt-in.
  const LANDING_PATHS = new Set(['/', '/dashboard']);
  function shouldAutoRedirectToMobile(toPath) {
    if (preference.value === 'desktop') return false;
    if (preference.value === 'mobile') return toPath !== '/mobile';
    if (!isMobileViewport.value) return false;
    if (toPath === '/mobile') return false;
    return LANDING_PATHS.has(toPath);
  }

  // MH-5 (audit P1 #3 — systemic desktop-table overflow on mobile):
  // Specific desktop routes have a card-stack mobile companion. When a
  // phone-viewport user lands on the desktop route, send them to the
  // companion. Distinct from `shouldAutoRedirectToMobile` because:
  //   - applies on every nav (not just landings) — the desktop /customers
  //     table is genuinely unusable on a phone, not "the user clicked away
  //     on purpose"
  //   - redirects to the route-specific companion, not the generic /mobile
  // Tech-role users are handled separately in router/index.js (they get
  // /mobile for ALL non-mobile routes — see the tech-redirect block).
  // This map is for non-tech roles (office/admin/owner) on mobile only.
  const MOBILE_COMPANION_PATHS = {
    '/customers': '/mobile/customers',
    // Doors for Sale: the office grid is a wide DataTable whose Publish button
    // 403s for a technician (needs listings.publish). The field screen is the
    // phone-shaped equivalent. Mapped HERE as well as in AppBottomNav's drawer
    // override, because the drawer is only one of the ways a tech arrives — a
    // bookmark, the back button, or the command palette all route through here.
    '/door-listings': '/mobile/door-listings',
    // Phone.com voicemail/calls + SMS (2026-08-03): the desktop calls
    // table + 640px detail dialog are exactly the wide-table problem this
    // map exists for. Companion routes carry the same nav.office frontend
    // gate as the desktop drawer entries.
    '/phone-com/calls': '/mobile/phone',
    '/phone-com/messages': '/mobile/sms',
    // NOTE: '/jobs' is NOT mapped. The reason recorded here used to be
    // "/mobile/jobs is tech-scoped and would hide office data"; that is out of
    // date — MobileJobsView has a company-wide scope toggle
    // (`/api/mobile/jobs?scope=company`). The live blocker is that it DEFAULTS
    // to "My jobs", so an office user would land on a near-empty list.
    //
    // NOT mapped either — and this is the interesting one. The 2026-08-12 phone
    // audit measured desktop /billing at 885px, /estimates 735px and /inventory
    // 595px on a 390px screen, and all three HAVE mobile companions that
    // measured clean. Adding them here looks obviously right and is a trap:
    //
    //   - `preference === 'desktop'` is the only escape hatch, and forceDesktop()
    //     is called from nowhere in the UI. A redirect here is therefore
    //     PERMANENT: a phone user could never reach the desktop view again.
    //   - the companions are narrower, not equivalent. MobileInventoryView is
    //     read-only (one GET) while desktop has create/edit/delete;
    //     convert-to-job exists in no Mobile* view; MobileBillingView has no
    //     delete-invoice, pay-link, or Ready-for-Billing dismissal verbs.
    //   - three call sites deep-link into /billing with a query the companion
    //     never reads (JobDetailView, JobsView, DashboardView). The guard below
    //     forwards `to.query`, but MobileBillingView ignores it, so
    //     `?action=create` — the Create Invoice button on a job — would land on
    //     a plain list and silently do nothing.
    //
    // The width problem is real; a redirect is the wrong cure. The right fix is
    // to card-stack those tables at phone width, which keeps every capability.
    //
    // NOTE: '/profile' not mapped — the responsive CSS clamp in MH-5
    // makes the desktop view fit at 390px without a separate companion.
  };
  function mobileCompanionFor(toPath) {
    if (preference.value === 'desktop') return null;
    if (!isMobileViewport.value && preference.value !== 'mobile') return null;
    return MOBILE_COMPANION_PATHS[toPath] || null;
  }

  return {
    preference,
    isMobileViewport,
    setPreference,
    forceDesktop: () => setPreference('desktop'),
    forceMobile: () => setPreference('mobile'),
    resetPreference: () => setPreference('auto'),
    shouldAutoRedirectToMobile,
    mobileCompanionFor,
  };
}
