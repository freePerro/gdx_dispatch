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
      @click="handleTab(item)"
    >
      <i :class="item.icon" aria-hidden="true" />
      <span>{{ item.label }}</span>
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
  </nav>
</template>

<script setup>
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Drawer from 'primevue/drawer';
import InputText from 'primevue/inputtext';
import { useTenantModules } from '../composables/useTenantModules';
import { useAuthStore } from '../stores/auth';
import { groupModulesForRole } from '../composables/useModuleSections';

const route = useRoute();
const router = useRouter();
const { allEnabledModules } = useTenantModules();
const auth = useAuthStore();

const moreOpen = ref(false);
const moreSearch = ref('');

function closeDrawer() {
  moreOpen.value = false;
  // Reset the filter when the drawer closes so opening it again next
  // time doesn't surprise the user with the previous search applied.
  moreSearch.value = '';
}

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
  const role = String(auth.user?.role || '').toLowerCase();
  // Backend canonical short role is 'tech' (per VALID_ROLES in users.py),
  // but the role-permissions UI uses 'technician'. Accept both so the
  // tech-shaped strip fires regardless of which form the JWT carries.
  const isTech = role === 'technician' || role === 'tech';
  const items = isTech
    ? [
        // MH-9b (Doug 2026-05-19): Photos is a per-job action techs do
        // many times a day; Profile is a once-a-month settings page.
        // Photos takes the primary slot, Profile demotes to More.
        { key: 'today', label: 'Today', icon: 'pi pi-calendar', to: '/mobile' },
        { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/mobile/jobs' },
        { key: 'timeclock', label: 'Clock', icon: 'pi pi-clock', to: '/mobile/timeclock' },
        { key: 'photos', label: 'Photos', icon: 'pi pi-images', to: '/photos' },
        { key: 'more', label: 'More', icon: 'pi pi-ellipsis-h', to: '' },
      ]
    : [
        { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/mobile/jobs' },
        { key: 'timeclock', label: 'Clock', icon: 'pi pi-clock', to: '/mobile/timeclock' },
        { key: 'planner', label: 'Planner', icon: 'pi pi-calendar-plus', to: '/mobile/planner' },
        { key: 'dispatch', label: 'Dispatch', icon: 'pi pi-map', to: '/mobile/dispatch' },
        { key: 'more', label: 'More', icon: 'pi pi-ellipsis-h', to: '' },
      ];
  return items.map((item) => ({
    ...item,
    available: item.key === 'more' ? true : routeExists(item.to),
  }));
});

const reservedKeys = new Set(['jobs', 'dispatch', 'customers', 'gps']);

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
  // /delivery-loadsheet stays on /delivery-loadsheet — it's already
  // mobile-friendly via @media rules in its scoped styles.
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
  '/jobs', // bottom-nav handles the mobile redirect at the view level
  '/dispatch',
  '/timeclock',
  '/delivery-loadsheet', // explicitly mobile-friendly per the comment above
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
  const role = String(auth.user?.role || '').toLowerCase();
  const isTech = role === 'technician' || role === 'tech';
  const base = allEnabledModules.value
    .filter((module) => !reservedKeys.has(module.key))
    .map((module) => {
      const resolvedTo = MOBILE_ROUTE_OVERRIDES[module.to] || module.to;
      return {
        ...module,
        to: resolvedTo,
        // mobile_friendly: true → render normally. false → render dimmed
        // with a "Desktop" pill so the tech knows the destination is
        // not phone-shaped.
        mobile_friendly: MOBILE_FRIENDLY_PATHS.has(resolvedTo),
      };
    });
  return isTech ? [...base, { ...PROFILE_DRAWER_ENTRY, mobile_friendly: true }] : base;
});

// MH-4: role-gate + group + search-filter via the shared composable.
// Empty section list means "no matches for the current search" — the
// drawer renders an empty-state hint in that case.
const moreSections = computed(() =>
  groupModulesForRole(moreModules.value, auth.user?.role, moreSearch.value),
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
  grid-template-columns: repeat(5, minmax(0, 1fr));
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
  background: var(--surface-muted, #e2e8f0);
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
