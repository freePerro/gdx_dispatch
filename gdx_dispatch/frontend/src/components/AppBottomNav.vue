<template>
  <nav class="bottom-nav" aria-label="Mobile navigation">
    <button
      v-for="item in tabItems"
      :key="item.key"
      type="button"
      class="tab-btn"
      :class="{ active: isRouteActive(item.to), disabled: !item.available }"
      :disabled="!item.available"
      :title="item.available ? null : 'Coming soon'"
      :aria-disabled="!item.available"
      :aria-label="item.badge ? `${item.label}, ${item.badge} unread` : null"
      :data-testid="`tab-${item.key}`"
      @click="handleTab(item)"
    >
      <span class="tab-icon">
        <i :class="item.icon" aria-hidden="true" />
        <span
          v-if="item.badge"
          class="tab-badge"
          aria-hidden="true"
          data-testid="email-unread-badge-mobile"
        >{{ item.badge }}</span>
      </span>
      <span class="tab-label">{{ item.label }}</span>
    </button>

    <Drawer v-model:visible="moreOpen" position="bottom" header="More Modules" class="more-drawer">
      <!-- MH-4 (audit P1 #5, P2 #22): pre-fix this drawer dumped all
           ~80 modules as a flat ungrouped list with no role gate and no
           search — admin/finance/platform items tappable by a field
           tech. Now: filter input + role-gated subset + 5-section
           grouping (Field / Customers & Comms / Money / Inventory /
           Admin). Payroll de-duped by (label, permission). -->
      <div class="drawer-search">
        <span class="p-icon-wrapper" aria-hidden="true"><i class="pi pi-search" /></span>
        <InputText
          v-model="moreSearch"
          placeholder="Filter modules…"
          aria-label="Filter modules"
          data-testid="more-search"
          autocomplete="off"
          spellcheck="false"
          autocapitalize="off"
        />
        <button
          v-if="moreSearch"
          type="button"
          class="drawer-search-clear"
          aria-label="Clear filter"
          @click="moreSearch = ''"
        ><i class="pi pi-times" aria-hidden="true" /></button>
      </div>

      <div v-if="!moreSections.length" class="drawer-empty" data-testid="more-empty">
        No modules match "{{ moreSearch }}".
      </div>

      <section
        v-for="bucket in moreSections"
        :key="bucket.section"
        class="drawer-section"
        :data-testid="`more-section-${bucket.section.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`"
      >
        <h3 class="drawer-section-heading">{{ bucket.section }}</h3>
        <div class="drawer-items">
          <router-link
            v-for="module in bucket.modules"
            :key="`${bucket.section}-${module.key}-${module.to}`"
            :to="module.to"
            class="drawer-link"
            :class="{ 'drawer-link--desktop-only': !module.mobile_friendly }"
            :title="module.mobile_friendly ? null : 'Best viewed on desktop'"
            @click="closeDrawer"
          >
            <i :class="module.icon" aria-hidden="true" />
            <span class="drawer-link-label">{{ module.label }}</span>
            <span
              v-if="!module.mobile_friendly"
              class="drawer-link-badge"
              aria-label="Desktop only"
            >Desktop</span>
          </router-link>
        </div>
      </section>
    </Drawer>

    <!-- Quick-capture FAB (2026-07-07): note a phone call in ~10s without
         stopping to find/create a customer. Office roles only — the same
         population that has the Planner tab. Floats above the nav, centered,
         clear of the bottom-right bug FAB. -->
    <button
      v-if="showCapture"
      type="button"
      class="capture-fab"
      aria-label="Quick note from a call"
      data-testid="quick-capture-fab"
      @click="captureOpen = true"
    >
      <i class="pi pi-plus" aria-hidden="true" />
    </button>
    <QuickCaptureSheet v-model:visible="captureOpen" :initial-note="captureSeed" @saved="onCaptureSaved" />
  </nav>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useToast } from 'primevue/usetoast';
import Drawer from 'primevue/drawer';
import InputText from 'primevue/inputtext';
import { useTenantModules } from '../composables/useTenantModules';
import { useAuthStore } from '../stores/auth';
import { useEmailUnreadStore } from '../stores/emailUnread';
import { groupModules } from '../composables/useModuleSections';
import { isTechnician } from '../constants/roles';
import QuickCaptureSheet from './QuickCaptureSheet.vue';

const route = useRoute();
const router = useRouter();
const toast = useToast();
const { allEnabledModules, isEnabled } = useTenantModules();
const auth = useAuthStore();
const emailUnread = useEmailUnreadStore();
// Role for shaping the strip: the persisted /api/users/me snapshot, else the
// JWT claim, which exists the instant a token does. A cold load with a token
// but no cached user painted the office strip — and, once the Email tab
// existed, polled the mailbox once — for a tech until /auth/me landed. The
// router learned the same lesson on 2026-08-28 (router/index.js, tech
// redirect block).
const effectiveRole = computed(() => auth.user?.role || auth.role);

const moreOpen = ref(false);
const moreSearch = ref('');

const captureOpen = ref(false);
// Office roles run the planner + field the calls; techs get a lean strip and
// don't. Gate the quick-capture FAB to the same non-tech population.
const showCapture = computed(() => !isTechnician(effectiveRole.value));

function onCaptureSaved() {
  // Nudge the planner to reload if it's mounted (e.g. Doug captured from the
  // planner screen). Harmless no-op elsewhere.
  window.dispatchEvent(new CustomEvent('gdx:planner-refresh'));
}

// ── PWA entry points (2026-08-03) ──
// The manifest's share_target lands Android shares on /mobile/planner with
// ?share_title/&share_text/&share_url; the "Quick note" app shortcut uses
// ?capture=1. Both open the capture sheet — shares with the text prefilled.
const captureSeed = ref('');
const SHARE_KEYS = ['share_title', 'share_text', 'share_url'];

function consumeCaptureParams() {
  const q = route.query;
  const wantsCapture = q.capture === '1' || SHARE_KEYS.some((k) => q[k] != null);
  if (!wantsCapture) return;
  if (!showCapture.value) {
    // Tech role: capture is office-only (same gate as the FAB). Never eat a
    // share silently — say why nothing happened. (Audit 2026-08-03 finding.)
    toast.add({
      severity: 'info',
      summary: 'Not captured',
      detail: 'Planner quick capture is available to office roles only.',
      life: 6000,
    });
  } else {
    // Title → text → url, skipping empties and anything already contained in
    // an earlier part (Android commonly repeats the URL inside the text).
    const parts = [];
    for (const key of SHARE_KEYS) {
      const v = typeof q[key] === 'string' ? q[key].trim() : '';
      if (v && !parts.some((p) => p.includes(v))) parts.push(v);
    }
    captureSeed.value = parts.join('\n');
    captureOpen.value = true;
  }
  // Strip the params either way so back/refresh doesn't re-open the sheet
  // (and a tech shared here isn't left with dead params in the URL).
  const rest = { ...q };
  delete rest.capture;
  for (const key of SHARE_KEYS) delete rest[key];
  router.replace({ path: route.path, query: rest });
}

onMounted(consumeCaptureParams);
// A share can also arrive while the installed app is already running — it
// lands as an in-app navigation, not a fresh mount.
watch(() => route.fullPath, consumeCaptureParams);
// One-shot seed: once the sheet closes, a manual FAB open starts blank.
watch(captureOpen, (v) => {
  if (!v) captureSeed.value = '';
});

function closeDrawer() {
  moreOpen.value = false;
  // Reset the filter when the drawer closes so opening it again next
  // time doesn't surprise the user with the previous search applied.
  moreSearch.value = '';
}

// ── Email tab (Doug, 2026-09-22: "office roles should have an easy access
// point to it from mobile") ──
// /mobile/inbox has existed since the first release, but the only way in was
// the More drawer: the office row's tabs were Jobs / Customers / Clock /
// Planner / Dispatch, and the Outlook inbox sat behind two taps and a scroll.
// Office roles get it as a tab, with the same unread badge the desktop
// sidebar pin carries. Techs keep it in the drawer (their strip is lean by
// design and the drawer entry is unchanged). Hidden when the tenant has the
// `email` module switched off — that is the backend registry key the Outlook
// routes gate on (there is no `inbox` module; the catalog entry carries
// `requires: 'email'` for the same reason). A tab into a disabled module is
// a dead end. Signed-in only: with no session there is no role to shape by,
// and the poll would just 401.
const showEmailTab = computed(
  () => auth.isAuthenticated && !isTechnician(effectiveRole.value) && isEnabled('email'),
);
const emailBadge = computed(() => {
  const n = emailUnread.count;
  if (!n || n <= 0) return '';
  return n > 99 ? '99+' : String(n);
});

// Poll only while the tab is showing. startPolling() hands back the release
// for OUR subscription: on a phone the sidebar (inside a lazy Drawer) mounts
// and unmounts with the hamburger, and releasing its own cannot touch ours.
let _releaseEmailPoll = null;
function _syncEmailPolling(show) {
  if (show && !_releaseEmailPoll) {
    _releaseEmailPoll = emailUnread.startPolling();
  } else if (!show && _releaseEmailPoll) {
    _releaseEmailPoll();
    _releaseEmailPoll = null;
  }
}
watch(showEmailTab, _syncEmailPolling, { immediate: true });
onUnmounted(() => _syncEmailPolling(false));

function routeExists(path) {
  if (!path) return false;
  try {
    return router.resolve(path).matched.length > 0;
  } catch {
    return false;
  }
}

const tabItems = computed(() => {
  // Role-shape the bottom nav. A field tech doesn't dispatch, doesn't
  // run the planner — those tabs are noise + cause confusion. The
  // tech-shaped strip is Today / Jobs / Clock / More. Office roles
  // (dispatcher / admin / owner / sales) keep the original strip.
  // isTechnician accepts both the short 'tech' (DB / VALID_ROLES) and long
  // 'technician' (role-permissions UI) spellings of the role.
  const isTech = isTechnician(effectiveRole.value);
  const items = isTech
    ? [
        // MH-9b (Doug 2026-05-19): Photos is a per-job action techs do
        // many times a day; Profile is a once-a-month settings page.
        // Photos takes the primary slot, Profile demotes to More.
        { key: 'today', label: 'Today', icon: 'pi pi-calendar', to: '/mobile' },
        { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/mobile/jobs' },
        // 2026-07-22 (Doug: "pain to add/search customers on mobile"):
        // Customers was reserved out of the More drawer (reservedKeys) but
        // its tab never got added — it was unreachable from the bottom nav
        // entirely. Both role rows get the tab; the grid CSS is
        // count-agnostic (grid-auto-columns) so 6 tabs lay out evenly.
        { key: 'customers', label: 'Customers', icon: 'pi pi-users', to: '/mobile/customers' },
        { key: 'timeclock', label: 'Clock', icon: 'pi pi-clock', to: '/mobile/timeclock' },
        { key: 'photos', label: 'Photos', icon: 'pi pi-images', to: '/photos' },
        { key: 'more', label: 'More', icon: 'pi pi-ellipsis-h', to: '' },
      ]
    : [
        { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/mobile/jobs' },
        { key: 'customers', label: 'Customers', icon: 'pi pi-users', to: '/mobile/customers' },
        { key: 'timeclock', label: 'Clock', icon: 'pi pi-clock', to: '/mobile/timeclock' },
        { key: 'planner', label: 'Planner', icon: 'pi pi-calendar-plus', to: '/mobile/planner' },
        { key: 'dispatch', label: 'Dispatch', icon: 'pi pi-map', to: '/mobile/dispatch' },
        // Appended rather than inserted so the five existing tabs keep their
        // positions (muscle memory). 7 tabs at 360px = 51px columns; the
        // 10px label rule below still fits "Customers" (48px).
        ...(showEmailTab.value
          ? [{ key: 'inbox', label: 'Email', icon: 'pi pi-envelope', to: '/mobile/inbox', badge: emailBadge.value }]
          : []),
        { key: 'more', label: 'More', icon: 'pi pi-ellipsis-h', to: '' },
      ];
  return items.map((item) => ({
    ...item,
    available: item.key === 'more' ? true : routeExists(item.to),
  }));
});

// Modules that already have a bottom-nav tab stay out of the More drawer.
// Role-aware on purpose: 'inbox' is a tab for office roles only, and
// reserving it for techs too would take away their only way in (the exact
// Customers mistake of 2026-07-22, in reverse).
const reservedKeys = computed(() => {
  const keys = new Set(['jobs', 'dispatch', 'customers', 'gps']);
  if (showEmailTab.value) keys.add('inbox');
  return keys;
});

// Modules whose canonical `to` is desktop-shaped but a mobile-shaped view
// exists. Rewrite for the More drawer so a tap from the bottom nav lands
// on the mobile view rather than the desktop one. Keeps modules.js as the
// single source of truth for the desktop sidebar.
const MOBILE_ROUTE_OVERRIDES = {
  '/planner': '/mobile/planner',
  '/customers': '/mobile/customers',
  '/inbox': '/mobile/inbox',
  '/estimates': '/mobile/estimates',
  '/billing': '/mobile/billing',
  '/inventory': '/mobile/inventory',
  '/parts-to-order': '/mobile/parts-to-order',
  // Doors for Sale has a purpose-built field screen: photo-first capture at
  // the tear-out, no pricing (the office sets that at approval).
  '/door-listings': '/mobile/door-listings',
  // Phone.com voicemail/calls + SMS companions (2026-08-03). The catalog
  // gates these entries on nav.office (frontend visibility shaping —
  // techs don't get them in the drawer; the API itself is not role-gated).
  '/phone-com/calls': '/mobile/phone',
  '/phone-com/messages': '/mobile/sms',
};

// Mobile-walk 2026-06-04 finding: the More drawer surfaced ~50 modules,
// but only the 7 above (+ a few like /jobs, /dispatch, /timeclock that
// are bottom-nav tabs) actually have phone-shaped views. Tapping
// "Scheduling" on a phone dropped the tech onto a wide desktop data
// table that clipped columns off-screen. This set names every module
// destination that is genuinely mobile-friendly; everything else gets
// flagged "desktop only" in the drawer so the tech knows what to
// expect before tapping.
const MOBILE_FRIENDLY_PATHS = new Set([
  '/mobile',
  '/mobile/jobs',
  '/mobile/dispatch',
  '/mobile/customers',
  '/mobile/timeclock',
  '/mobile/planner',
  '/mobile/inbox',
  '/mobile/estimates',
  '/mobile/billing',
  '/mobile/inventory',
  '/mobile/parts-to-order',
  '/mobile/door-listings',
  '/mobile/phone',
  '/mobile/sms',
  '/jobs', // bottom-nav handles the mobile redirect at the view level
  '/dispatch',
  '/timeclock',
  '/profile',
  '/photos',
]);

// MH-9b: Profile isn't in the module catalog (it's a fixed user-account
// route). For techs we want it surfaced in the More drawer since it no
// longer has a bottom-nav slot (Photos took its place). Office roles
// reach Profile via the header user menu like before.
const PROFILE_DRAWER_ENTRY = {
  key: 'profile',
  label: 'Profile',
  icon: 'pi pi-user',
  to: '/profile',
};

const moreModules = computed(() => {
  const isTech = isTechnician(effectiveRole.value);
  const base = allEnabledModules.value
    .filter((module) => !reservedKeys.value.has(module.key))
    .map((module) => {
      const resolvedTo = MOBILE_ROUTE_OVERRIDES[module.to] || module.to;
      return {
        ...module,
        to: resolvedTo,
        // mobile_friendly: true → render normally. false → render dimmed
        // with a "Desktop" pill so the tech knows the destination is
        // not phone-shaped.
        //
        // An entry may decide this for itself — plugin entries do, from their
        // UI manifest (useTenantModules `pluginMobileFriendly`), because their
        // `/plugins/<key>` path is only known at runtime and could never match
        // the literal-path set below. Everything else falls back to the set.
        // Note this is a `??`, not a truthiness check: `false` is a real answer
        // and must not fall through to the lookup.
        mobile_friendly: module.mobile_friendly ?? MOBILE_FRIENDLY_PATHS.has(resolvedTo),
      };
    });
  return isTech ? [...base, { ...PROFILE_DRAWER_ENTRY, mobile_friendly: true }] : base;
});

// Group + search-filter via the shared composable. Visibility is already
// permission-filtered upstream (allEnabledModules), so this only organizes.
// Empty section list means "no matches for the current search" — the
// drawer renders an empty-state hint in that case.
const moreSections = computed(() =>
  groupModules(moreModules.value, moreSearch.value),
);

function isRouteActive(targetPath) {
  if (!targetPath) {
    return false;
  }

  const path = route?.path ?? '';
  return path === targetPath || path.startsWith(`${targetPath}/`);
}

function handleTab(item) {
  if (item.key === 'more') {
    moreOpen.value = true;
    return;
  }

  if (!item.available) {
    return;
  }

  router.push(item.to);
}
</script>

<style scoped>
.bottom-nav {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  height: var(--bottom-nav-height);
  display: grid;
  /* Count-agnostic: every direct child gets an equal column, so adding a
     tab (Customers, 2026-07-22) doesn't leave a dead 6th-of-the-bar. */
  grid-auto-flow: column;
  grid-auto-columns: minmax(0, 1fr);
  background: var(--surface-header);
  border-top: 1px solid var(--border-subtle);
  z-index: 120;
}

.tab-btn {
  border: none;
  background: transparent;
  color: var(--text-muted);
  display: grid;
  place-items: center;
  gap: 0.125rem;
  font-size: 0.6875rem;
  cursor: pointer;
  /* 6 tabs at 360px = 60px each (7 with the office Email tab = 51px);
     "Customers" must ellipsize, not wrap or blow the column width. */
  min-width: 0;
  /* The UA stylesheet gives a <button> 6px of side padding. At 7 columns
     that took the 55.7px column down to a 44px label box and "Customers"
     (48px at 10px) ellipsized again — measured in the rendered DOM
     2026-09-22. The label gets the whole column. */
  padding: 0;
}

/* Unread badge on the Email tab: pinned to the icon's top-right corner so
   the label row keeps its ellipsis math. Same colour as the sidebar pin
   badge; the ring in the nav surface colour keeps it legible when the
   active-tab icon is the same blue. */
.tab-icon {
  position: relative;
  display: inline-grid;
  place-items: center;
}
.tab-badge {
  position: absolute;
  top: -0.4rem;
  right: -0.7rem;
  min-width: 1rem;
  height: 1rem;
  padding: 0 0.25rem;
  border-radius: 0.5rem;
  background: var(--interactive-primary, #2563eb);
  /* Text in the nav surface colour: white on brand blue in light mode,
     navy on the lighter dark-mode blue — white there measured ~2.3:1 on a
     16px badge (throwaway walk 2026-09-22). */
  color: var(--surface-header, #fff);
  font-size: 0.6rem;
  font-weight: 700;
  line-height: 1rem;
  text-align: center;
  box-shadow: 0 0 0 1.5px var(--surface-header);
  pointer-events: none;
}

.tab-btn .tab-label {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 6 tabs on a narrow phone: 390px/6 = 65px columns. "Customers" at the
   default 11px measures ~72px and ellipsizes ("Custom…"); at 10px it
   measures 48px and fits. Verified against the rendered DOM 2026-07-22.
   Office roles now have 7 (Email, 2026-09-22): 390px/7 = 55px and
   360px/7 = 51px, both still clear of the 48px label. */
@media (max-width: 430px) {
  .tab-btn {
    font-size: 0.625rem;
  }
}

.tab-btn i {
  font-size: 1.25rem;
}

.tab-btn {
  min-height: 52px; /* Larger touch target for gloved/wet hands */
}

.tab-btn.active {
  color: var(--interactive-primary);
}

.tab-btn.disabled,
.tab-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Quick-capture FAB: docked above the nav, centered. 56px is the Material
   touch-target size; sits clear of the bottom-right bug FAB. */
.capture-fab {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: calc(var(--bottom-nav-height) + var(--capture-fab-gap, 0.75rem));
  width: var(--capture-fab-size, 56px);
  height: var(--capture-fab-size, 56px);
  border-radius: 50%;
  border: none;
  background: var(--interactive-primary);
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 1.4rem;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
  z-index: 121;
  cursor: pointer;
}
.capture-fab:active {
  transform: translateX(-50%) scale(0.94);
}

.drawer-items {
  display: grid;
  gap: var(--space-2);
}

.drawer-link {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  text-decoration: none;
  color: var(--text-primary);
  background: var(--surface-elevated);
  border-radius: 0.625rem;
  padding: var(--space-3);
}
.drawer-link-label {
  flex: 1 1 auto;
  min-width: 0;
}
/* Mobile-walk 2026-06-04: modules without a phone-shaped view get
   dimmed + a "Desktop" pill so the tech knows what they're tapping
   into before they land on a clipped wide table. The link still
   routes — we don't want to BLOCK access, just label it. */
.drawer-link--desktop-only {
  opacity: 0.7;
}
.drawer-link-badge {
  flex: 0 0 auto;
  font-size: 0.65rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.15rem 0.4rem;
  border-radius: 0.375rem;
  background: var(--p-content-hover-background);
  color: var(--text-muted, #64748b);
}

/* MH-4: drawer search input + sticky section headers. The headers are
   sticky inside the drawer body so a long Field/Customers list keeps
   its label visible as the user scrolls. */
.drawer-search {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 0 0 var(--space-2);
  background: var(--surface-panel);
}
.drawer-search :deep(.p-inputtext) {
  flex: 1 1 auto;
  min-width: 0;
}
.drawer-search .p-icon-wrapper {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.5rem;
  height: 1.5rem;
  color: var(--text-muted);
}
.drawer-search-clear {
  background: none;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0.25rem 0.5rem;
  font-size: 1rem;
  min-width: 44px;
  min-height: 44px;
}
.drawer-empty {
  padding: var(--space-3);
  color: var(--text-muted);
  text-align: center;
  font-size: 0.85rem;
}
.drawer-section {
  margin-top: var(--space-3);
}
.drawer-section:first-of-type {
  margin-top: 0;
}
.drawer-section-heading {
  position: sticky;
  top: 3rem; /* sits below the search bar */
  z-index: 1;
  margin: 0 0 var(--space-2);
  padding: 0.25rem 0;
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
  background: var(--surface-panel);
}

:deep(.more-drawer .p-drawer-content) {
  background: var(--surface-panel);
}
</style>

<!-- The More-drawer rules below are deliberately NOT scoped. PrimeVue
     teleports the Drawer to <body>, so it sits outside the AppBottomNav
     component subtree and the [data-v-hash] attribute selector that Vue
     injects for scoped + :deep() rules never matches the rendered DOM.
     Plus the `class="more-drawer"` prop lands on the .p-drawer panel
     itself (verified in primevue/drawer/style/index.mjs — `cx('root')`
     returns ['p-drawer p-component', ...] and ptmi('root') merges the
     parent's class), so the height override has to target the panel
     class, not a descendant. -->
<style>
/* PrimeVue Drawer position="bottom" defaults to height: 10rem (~160px) per
   @primeuix/styles/drawer (specificity 0,2,0: `.p-drawer-bottom .p-drawer`).
   On a phone that's ~20% of the viewport — Doug 2026-05-10: "you only see
   a little bit of the screen." The mask + panel chain below is 0,4,0,
   which beats the default unambiguously regardless of stylesheet order. */
.p-drawer-mask.p-drawer-bottom .more-drawer.p-drawer {
  /* iOS Safari < 15.4 doesn't support `dvh`. CSS calc() invalidates the
     whole expression if any unit is unsupported, so the first declaration
     is a `vh`-only fallback that always parses; the second upgrades to
     dvh on browsers that support it. */
  height: min(80vh, calc(100vh - var(--bottom-nav-height, 60px) - 1rem));
  height: min(80vh, calc(100dvh - var(--bottom-nav-height, 60px) - 1rem));
  max-height: 90vh;
  max-height: 90dvh;
}
</style>
