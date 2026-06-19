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
    // NOTE: '/jobs' is intentionally NOT mapped here — /mobile/jobs is
    // tech-scoped ("My Jobs") and would hide office/admin data. See
    // MH-5b follow-up for a non-tech-scoped /mobile/jobs-list.
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
