<template>
  <div
    class="app-layout"
    :class="{
      collapsed: sidebarCollapsed,
      mobile: isMobile,
      'mobile-sidebar-open': mobileSidebarOpen,
    }"
  >
    <!-- 2026-07-01 a11y audit: keyboard/screen-reader users had to tab
         through the whole sidebar on every page. Visible on focus only. -->
    <a href="#main-content" class="skip-link">Skip to main content</a>
    <AppSidebar
      v-if="!isMobile"
      :collapsed="sidebarCollapsed"
      @toggle-collapse="toggleSidebarCollapse"
      @item-selected="closeMobileSidebar"
    />

    <Drawer
      v-else
      v-model:visible="mobileSidebarOpen"
      position="left"
      class="mobile-sidebar-drawer"
      :show-close-icon="false"
    >
      <AppSidebar
        mobile
        :mobile-open="mobileSidebarOpen"
        :collapsed="false"
        @toggle-collapse="toggleSidebarCollapse"
        @item-selected="closeMobileSidebar"
      />
    </Drawer>

    <div class="layout-main">
      <div class="layout-header">
        <AppTopbar
          @toggle-navigation="handleNavigationToggle"
          @open-search="openCommandPalette"
          @show-notifications="notificationsOpen = true"
        />
        <!-- Breadcrumb removed sitewide 2026-05-03: desktop wayfinding crutch
             that ate ~32px on every screen. Mobile uses bottom-nav + page
             title; desktop uses the sidebar. Title is on each view. -->
      </div>

      <main id="main-content" class="layout-content" tabindex="-1">
        <slot />
      </main>
    </div>

    <AppBottomNav v-if="isMobile" />
    <HelpDrawer />
    <!-- MH-1 (mobile hardening, audit P1 #11): the 🐛 bug-report FAB
         shipped to production for every role, overlapping content/nav
         on every mobile screen. Gated now: visible only when the build
         is opted in via `VITE_SHOW_DEBUG_FAB=1` OR the signed-in user
         is the `owner` role (Doug). Hides for tech + admin + everyone
         else by default. Re-enable globally by setting the env flag at
         build time. -->
    <BugReportButton v-if="showDebugFab" />
    <ConfirmDialog />
    <NotificationsDrawer v-model="notificationsOpen" />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import Breadcrumb from 'primevue/breadcrumb';
import ConfirmDialog from 'primevue/confirmdialog';
import Drawer from 'primevue/drawer';
import AppSidebar from './AppSidebar.vue';
import AppTopbar from './AppTopbar.vue';
import AppBottomNav from './AppBottomNav.vue';
import HelpDrawer from './HelpDrawer.vue';
import BugReportButton from './BugReportButton.vue';
import NotificationsDrawer from './NotificationsDrawer.vue';
import { useAuthStore } from '../stores/auth';
import { useTour } from '../composables/useTour';

const route = useRoute();

const sidebarCollapsed = ref(false);
const mobileSidebarOpen = ref(false);
const isMobile = ref(false);
const notificationsOpen = ref(false);

const auth = useAuthStore();
const tour = useTour();

// MH-1: bug-report FAB visibility. Default OFF in prod build (env unset).
// Enabled when:
//  - Build flag `VITE_SHOW_DEBUG_FAB=1` (dev/staging or explicit prod opt-in), OR
//  - Signed-in user is the `owner` role (per-user opt-in for Doug et al.).
// `admin` and `tech` no longer see the button by default (audit P1 #11).
const showDebugFab = computed(() => {
  if (import.meta.env.VITE_SHOW_DEBUG_FAB === '1') return true;
  return String(auth.role || auth.user?.role || '').toLowerCase() === 'owner';
});

onMounted(() => {
  // Auto-launch the role-matched tour on dashboard if the user hasn't
  // seen it yet. Delayed so the help button + sidebar anchors are
  // rendered. Tours degrade gracefully if no anchors resolve.
  setTimeout(() => {
    const path = route?.path ?? '';
    if (path !== '/dashboard' && path !== '/') return;
    const role = String(auth.user?.role || '').toLowerCase();
    if (!role) return;
    tour.autoLaunchForUser({ role });
  }, 1200);
});

const homeCrumb = computed(() => ({ icon: 'pi pi-home', to: '/dashboard', label: 'Home' }));

const breadcrumbItems = computed(() => {
  return route.matched
    .filter((record) => record.name && record.name !== 'login')
    .map((record) => ({
      label: formatRouteLabel(String(record.name)),
      to: record.path,
    }));
});

const _ACRONYMS = { gps: 'GPS', sso: 'SSO', api: 'API', gdpr: 'GDPR', qbo: 'QBO', pdf: 'PDF' };

function formatRouteLabel(value) {
  return value
    .replace(/-/g, ' ')
    .split(' ')
    .map((word) => _ACRONYMS[word.toLowerCase()] || word.replace(/^\w/, (c) => c.toUpperCase()))
    .join(' ');
}

function closeMobileSidebar() {
  mobileSidebarOpen.value = false;
}

function toggleSidebarCollapse() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
}

function handleNavigationToggle() {
  if (isMobile.value) {
    mobileSidebarOpen.value = !mobileSidebarOpen.value;
    return;
  }

  toggleSidebarCollapse();
}

function openCommandPalette() {
  window.dispatchEvent(new CustomEvent('gdx:open-command-palette'));
}

function applyMediaState() {
  const width = window.innerWidth;
  isMobile.value = width < 768;

  if (isMobile.value) {
    sidebarCollapsed.value = false;
  } else {
    mobileSidebarOpen.value = false;
    if (width < 1100) {
      sidebarCollapsed.value = true;
    }
  }
}

onMounted(() => {
  applyMediaState();
  window.addEventListener('resize', applyMediaState);
});

onUnmounted(() => {
  window.removeEventListener('resize', applyMediaState);
});
</script>

<style scoped>
/* Skip link: visually hidden until keyboard-focused. */
.skip-link {
  position: absolute;
  top: -100px;
  left: 0.75rem;
  z-index: 3000;
  padding: 0.5rem 1rem;
  border-radius: 0 0 0.5rem 0.5rem;
  background: var(--interactive-primary);
  color: var(--color-bg-900);
  font-weight: 600;
  text-decoration: none;
  transition: top 0.15s ease;
}
.skip-link:focus-visible {
  top: 0;
}
.layout-content:focus {
  outline: none;
}

.app-layout {
  height: 100vh;
  display: grid;
  grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
  background: var(--surface-app);
  color: var(--text-primary);
  overflow: hidden;
}

.app-layout.collapsed {
  grid-template-columns: var(--sidebar-collapsed-width) minmax(0, 1fr);
}

.app-layout.mobile {
  grid-template-columns: minmax(0, 1fr);
}

.layout-main {
  min-width: 0;
  height: 100vh;
  overflow: hidden;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}

.layout-header {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--surface-header);
}

.breadcrumbs-row {
  height: var(--breadcrumb-height);
  display: flex;
  align-items: center;
  padding: 0 var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--surface-header);
}

.layout-content {
  overflow: auto;
  padding: var(--space-4);
  padding-bottom: calc(var(--space-4) + var(--bottom-nav-height));
}

:deep(.p-breadcrumb) {
  border: none;
  background: transparent;
  padding: 0;
}

:deep(.p-drawer.mobile-sidebar-drawer) {
  width: min(20rem, 88vw);
}

:deep(.mobile-sidebar-drawer .p-drawer-content) {
  padding: 0;
  background: var(--surface-sidebar);
}

@media (min-width: 768px) {
  .layout-content {
    padding-bottom: var(--space-4);
  }
}
</style>
