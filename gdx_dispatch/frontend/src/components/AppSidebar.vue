<template>
  <aside
    class="app-sidebar"
    :class="{
      collapsed,
      'mobile-sidebar': mobile,
      'mobile-open': mobile && mobileOpen,
    }"
  >
    <div class="sidebar-header">
      <div class="tenant-brand" :title="branding.company_name">
        <img
          v-if="branding.logo_url"
          :src="branding.logo_url"
          :alt="branding.company_name"
          class="tenant-logo"
        />
        <div v-else class="tenant-logo fallback">GDX</div>
        <div v-if="!collapsed" class="tenant-text">
          <strong>{{ branding.company_name }}</strong>
          <small>{{ branding.subtitle || 'Operations Console' }}</small>
        </div>
      </div>
      <Button
        type="button"
        :icon="collapsed ? 'pi pi-bars' : 'pi pi-angle-double-left'"
        class="p-button-text collapse-toggle"
        :class="{ collapsed }"
        @click="$emit('toggle-collapse')"
        :aria-label="collapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        :title="collapsed ? 'Expand sidebar' : 'Collapse sidebar'"
      />
    </div>

    <div v-if="!collapsed" class="sidebar-groups">
      <!-- Always-visible Home pin. DT-2: tech sees "Today" → /mobile
           (MobileTodayView is the tech's home; /dashboard is admin-shape
           and 403s on tech-owned API calls). Office/admin keep "Dashboard"
           → /dashboard. -->
      <router-link
        :to="homePin.to"
        class="menu-item pinned-item"
        :class="{ active: isActiveRoute(homePin.to) }"
        @click="handleItemClick(homePin.to, homePin.label, homePin.icon)"
        data-testid="sidebar-dashboard"
        data-tour="nav-dashboard"
      >
        <i :class="homePin.icon" aria-hidden="true" />
        <span>{{ homePin.label }}</span>
      </router-link>

      <!-- Communication quick-pins (only when the matching integration is on). -->
      <router-link
        v-for="pin in topPins"
        :key="`pin-${pin.to}`"
        :to="pin.to"
        class="menu-item pinned-item"
        :class="{ active: isActiveRoute(pin.to) }"
        @click="handleItemClick(pin.to, pin.label, pin.icon)"
        :data-testid="`sidebar-pin-${pin.key}`"
      >
        <i :class="pin.icon" aria-hidden="true" />
        <span>{{ pin.label }}</span>
      </router-link>

      <!-- Search/filter -->
      <div class="sidebar-search">
        <i class="pi pi-search search-icon" aria-hidden="true" />
        <input
          v-model="filterText"
          type="text"
          placeholder="Find module... (Ctrl+/)"
          class="sidebar-search-input"
          aria-label="Filter sidebar modules"
          data-testid="sidebar-search"
          ref="searchInputRef"
          @keydown.esc="filterText = ''"
        />
        <button
          v-if="filterText"
          class="search-clear"
          aria-label="Clear search"
          @click="filterText = ''"
          data-testid="sidebar-search-clear"
        >
          <i class="pi pi-times" />
        </button>
      </div>

      <!-- Filter results: flat list when searching -->
      <div v-if="filterText" class="filter-results" data-testid="sidebar-filter-results">
        <p v-if="!filteredFlat.length" class="filter-empty">No modules match "{{ filterText }}"</p>
        <div
          v-for="m in filteredFlat"
          :key="m.key"
          class="menu-row"
          :class="{ active: isActiveRoute(m.to) }"
        >
          <router-link
            :to="m.to"
            class="menu-item menu-item-link"
            @click="handleItemClick(m.to, m.label, m.icon)"
          >
            <i :class="m.icon" aria-hidden="true" />
            <span>{{ m.label }}</span>
          </router-link>
          <button
            class="fav-toggle"
            :class="{ active: isFavorite(m.to) }"
            :aria-label="isFavorite(m.to) ? `Unfavorite ${m.label}` : `Favorite ${m.label}`"
            :title="isFavorite(m.to) ? 'Remove from favorites' : 'Add to favorites'"
            @click.stop.prevent="toggleFavorite(m.to, m.label, m.icon)"
          >
            <i :class="isFavorite(m.to) ? 'pi pi-star-fill' : 'pi pi-star'" />
          </button>
        </div>
      </div>

      <!-- Favorites (only when not searching, only if any) -->
      <div v-if="!filterText && favoriteModules.length" class="favorites-section" data-testid="sidebar-favorites">
        <div class="section-label">Favorites</div>
        <div
          v-for="m in favoriteModules"
          :key="`fav-${m.to}`"
          class="menu-row"
          :class="{ active: isActiveRoute(m.to) }"
        >
          <router-link
            :to="m.to"
            class="menu-item menu-item-link"
            @click="handleItemClick(m.to, m.label, m.icon)"
          >
            <i :class="m.icon" aria-hidden="true" />
            <span>{{ m.label }}</span>
          </router-link>
          <button
            class="fav-toggle active"
            :aria-label="`Unfavorite ${m.label}`"
            title="Remove from favorites"
            @click.stop.prevent="toggleFavorite(m.to, m.label, m.icon)"
          >
            <i class="pi pi-star-fill" />
          </button>
        </div>
      </div>

      <!-- Categorized panel (hidden during search) -->
      <PanelMenu v-if="!filterText" :model="panelItems" multiple>
        <template #item="{ item }">
          <div
            v-if="item.to"
            class="menu-row"
            :class="{ active: isActiveRoute(item.to) }"
          >
            <router-link
              :to="item.to"
              class="menu-item menu-item-link"
              @click="handleItemClick(item.to, item.label, item.icon)"
            >
              <i v-if="item.icon" :class="item.icon" aria-hidden="true" />
              <span>{{ item.label }}</span>
            </router-link>
            <button
              class="fav-toggle"
              :class="{ active: isFavorite(item.to) }"
              :aria-label="isFavorite(item.to) ? `Unfavorite ${item.label}` : `Favorite ${item.label}`"
              :title="isFavorite(item.to) ? 'Remove from favorites' : 'Add to favorites'"
              @click.stop.prevent="toggleFavorite(item.to, item.label, item.icon)"
            >
              <i :class="isFavorite(item.to) ? 'pi pi-star-fill' : 'pi pi-star'" />
            </button>
          </div>
          <div v-else class="menu-group-header">
            <i v-if="item.icon" :class="item.icon" aria-hidden="true" />
            <span>{{ item.label }}</span>
          </div>
        </template>
      </PanelMenu>
    </div>

    <div v-else class="sidebar-icons">
      <router-link
        v-for="module in roleAllowedModules"
        :key="module.key"
        :to="module.to"
        class="icon-item"
        :class="{ active: isActiveRoute(module.to) }"
        :title="module.label"
        @click="handleItemClick"
      >
        <i :class="module.icon" aria-hidden="true" />
      </router-link>
    </div>

    <button
      type="button"
      class="sidebar-tour-replay"
      :class="{ collapsed }"
      data-tour="tour-replay"
      data-test="sidebar-tour-replay"
      :title="collapsed ? 'Take the tour' : ''"
      @click="launchTour"
    >
      <i class="pi pi-compass" aria-hidden="true" />
      <span v-if="!collapsed">Take the tour</span>
    </button>

    <div
      class="sidebar-build-stamp"
      :title="`build ${buildSha} · built ${buildTime}`"
      data-testid="sidebar-build-stamp"
    >
      <span v-if="!collapsed">{{ versionLabel }}</span>
      <span v-else>{{ versionLabelShort }}</span>
    </div>
  </aside>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { storeToRefs } from 'pinia';
import { useRoute } from 'vue-router';
import Button from 'primevue/button';
import PanelMenu from 'primevue/panelmenu';
import { useThemeStore } from '../stores/theme';
import { useAuthStore } from '../stores/auth';
import { useTenantModules } from '../composables/useTenantModules';
import { isModuleAllowedForRole } from '../composables/useModuleSections';
import { useTour } from '../composables/useTour';
import { resolveInitialFavorites } from '../utils/sidebarFavorites';

const FAVORITES_KEY = 'gdx_sidebar_favorites';

const props = defineProps({
  collapsed: {
    type: Boolean,
    default: false,
  },
  mobile: {
    type: Boolean,
    default: false,
  },
  mobileOpen: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(['toggle-collapse', 'item-selected']);

const route = useRoute();
const theme = useThemeStore();
const auth = useAuthStore();
const tour = useTour();
const { branding } = storeToRefs(theme);
const { categories, allEnabledModules, enabledModules } = useTenantModules();

// DT-1: role-gate every module surface this component renders. The mobile
// More drawer already does this via `groupModulesForRole`; the desktop
// sidebar was pulling raw `allEnabledModules` and bypassing the gate, so
// a tech logging in on desktop saw the full ~50-module list (Payroll,
// QuickBooks, Admin Ops, etc.).
const currentRole = computed(() => auth.user?.role || '');
const roleAllowedModules = computed(() =>
  allEnabledModules.value.filter((m) => isModuleAllowedForRole(m.key, currentRole.value)),
);
// DT-2: tech home is /mobile (MobileTodayView), NOT /dashboard. Same gate
// as `_normalizeRole` — `technician` aliases to `tech`.
const homePin = computed(() => {
  const r = String(currentRole.value || '').toLowerCase();
  if (r === 'tech' || r === 'technician') {
    return { to: '/mobile', label: 'Today', icon: 'pi pi-home' };
  }
  return { to: '/dashboard', label: 'Dashboard', icon: 'pi pi-home' };
});

const roleAllowedCategories = computed(() =>
  categories.value
    .map((category) => ({
      ...category,
      modules: category.modules.filter((m) => isModuleAllowedForRole(m.key, currentRole.value)),
    }))
    .filter((category) => category.modules.length > 0),
);

function launchTour() {
  const role = String(auth.user?.role || '').toLowerCase();
  const tourId = tour.defaultTourFor(role);
  if (!tourId) return;
  tour.launch(tourId, { force: true });
  emit('item-selected');
}

// Communication quick-pins below Dashboard. Each appears only when the
// underlying module entry is enabled for the tenant (which already gates on
// `requires: phone_com` for the phone/sms entries via useTenantModules).
const topPins = computed(() => {
  const wanted = [
    { key: 'inbox', label: 'Inbox', icon: 'pi pi-inbox', to: '/inbox' },
    { key: 'phone_com_calls', label: 'Phone', icon: 'pi pi-phone', to: '/phone-com/calls' },
    { key: 'phone_com_messages', label: 'SMS', icon: 'pi pi-comment', to: '/phone-com/messages' },
  ];
  const enabled = new Set(roleAllowedModules.value.map((m) => m.key));
  return wanted.filter((p) => enabled.has(p.key));
});

const panelItems = computed(() => {
  return roleAllowedCategories.value.map((category) => ({
    key: category.key,
    label: category.label,
    icon: category.icon,
    items: category.modules.map((module) => ({
      key: module.key,
      label: module.label,
      icon: module.icon,
      to: module.to,
    })),
  }));
});

function isActiveRoute(targetPath) {
  const path = route?.path ?? '';
  return path === targetPath || path.startsWith(`${targetPath}/`);
}

// --- Filter (search) ---
const filterText = ref('');
const searchInputRef = ref(null);

const filteredFlat = computed(() => {
  const q = filterText.value.trim().toLowerCase();
  if (!q) return [];
  return roleAllowedModules.value.filter((m) =>
    m.label.toLowerCase().includes(q) || m.key.toLowerCase().includes(q)
  );
});

// Ctrl+/ or Cmd+/ focuses the search box
function onKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key === '/') {
    event.preventDefault();
    if (searchInputRef.value) searchInputRef.value.focus();
  }
}

// --- Favorites ---
const favorites = ref([]);

function loadFavorites() {
  let raw = null;
  try { raw = localStorage.getItem(FAVORITES_KEY); } catch { raw = null; }
  const { favorites: initial, shouldPersist } = resolveInitialFavorites(raw);
  favorites.value = initial;
  if (shouldPersist) persistFavorites();
}

function persistFavorites() {
  try { localStorage.setItem(FAVORITES_KEY, JSON.stringify(favorites.value)); } catch { /* quota / private */ }
}

function isFavorite(to) {
  return favorites.value.some((f) => f.to === to);
}

function toggleFavorite(to, label, icon) {
  // Home pin (Dashboard for office/admin, Today for tech) is already pinned —
  // never favoritable.
  if (!to || to === homePin.value.to) return;
  if (isFavorite(to)) {
    favorites.value = favorites.value.filter((f) => f.to !== to);
  } else {
    favorites.value = [...favorites.value, { to, label, icon }];
  }
  persistFavorites();
}

// DT-1: favorites are stored in localStorage and may include modules the
// current role isn't allowed (e.g. former dispatcher demoted to tech).
// Filter at render so the rendered favorites match the rest of the sidebar;
// leave the stored list alone (no destructive mutation on role change).
const favoriteModules = computed(() => {
  const allowedRoutes = new Set(roleAllowedModules.value.map((m) => m.to));
  return favorites.value.filter((f) => allowedRoutes.has(f.to));
});

onMounted(() => {
  loadFavorites();
  window.addEventListener('keydown', onKeydown);
});
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown);
});

function handleItemClick(_to, _label, _icon) {
  filterText.value = '';
  emit('item-selected');
}

const buildSha = typeof __BUILD_SHA__ !== 'undefined' ? __BUILD_SHA__ : 'dev';
const buildTime = typeof __BUILD_TIME__ !== 'undefined' ? __BUILD_TIME__ : '';

// Show the deployed RELEASE version (APP_VERSION, from /pwa/version) rather than
// the raw git SHA. Falls back to the build SHA if the version is unknown ("dev")
// or the fetch fails. The SHA + build time stay in the hover title for debugging.
const releaseVersion = ref('');
const versionLabel = computed(() =>
  releaseVersion.value && releaseVersion.value !== 'dev'
    ? `v${releaseVersion.value}` : `build ${buildSha}`);
const versionLabelShort = computed(() =>
  releaseVersion.value && releaseVersion.value !== 'dev'
    ? `v${releaseVersion.value}` : buildSha);

onMounted(async () => {
  try {
    const r = await fetch('/pwa/version');
    if (r.ok) releaseVersion.value = (await r.json())?.version || '';
  } catch { /* leave blank → falls back to build SHA */ }
});
</script>

<style scoped>
.app-sidebar {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--surface-sidebar);
  border-right: 1px solid var(--border-subtle);
  transition: width var(--transition-fast);
  width: var(--sidebar-width);
  color: var(--text-primary);
}

/* Force PrimeVue text-button icons (collapse toggle) to show.
   PrimeVue v4 inherits from its own theme tokens which were
   leaving the icon invisible on the sidebar surface. */
.app-sidebar :deep(.p-button.p-button-text) {
  color: var(--text-primary);
  background: transparent;
}
.app-sidebar :deep(.p-button.p-button-text:hover) {
  color: var(--text-primary);
  background: var(--surface-hover);
}
.app-sidebar :deep(.p-button.p-button-text .pi) {
  color: var(--text-primary);
}

.app-sidebar.collapsed {
  width: var(--sidebar-collapsed-width);
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
}

/* When collapsed (64px wide), the 32px logo fills the interior and the
   toggle button got pushed off-screen — leaving the user with no
   visible way to expand. Stack vertically so both are visible, and the
   toggle is centered + bright. */
.app-sidebar.collapsed .sidebar-header {
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-2);
}

.tenant-brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.tenant-logo {
  width: 2rem;
  height: 2rem;
  border-radius: 0.5rem;
  object-fit: contain;
  background: var(--surface-elevated);
}

.tenant-logo.fallback {
  display: grid;
  place-items: center;
  color: var(--text-primary);
  font-size: 0.6875rem;
  font-weight: 700;
}

.tenant-text {
  display: grid;
  min-width: 0;
}

.tenant-text strong {
  color: var(--text-primary);
  font-size: 0.875rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tenant-text small {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.collapse-toggle {
  color: var(--text-primary);
}

/* When collapsed, swap the icon to pi-bars (no rotation needed) and
   make the affordance more prominent so a user who accidentally
   collapses can find their way back. */
.collapse-toggle.collapsed {
  background: var(--surface-elevated);
  border: 1px solid var(--border-subtle);
}
.collapse-toggle.collapsed:hover {
  background: var(--surface-hover);
}

.sidebar-groups {
  flex: 1;
  overflow: auto;
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.pinned-item {
  background: var(--surface-elevated, transparent);
  border: 1px solid var(--border-subtle);
  font-weight: 600;
}
.pinned-item:hover {
  background: var(--surface-hover);
}

.sidebar-search {
  position: relative;
  display: flex;
  align-items: center;
  background: var(--surface-elevated, var(--p-content-hover-background));
  border: 1px solid var(--border-subtle);
  border-radius: 0.5rem;
  padding: 0 0.5rem;
}
.sidebar-search:focus-within {
  border-color: var(--interactive-primary, var(--p-primary-color));
}
.sidebar-search .search-icon {
  font-size: 0.85rem;
  color: var(--text-muted);
}
.sidebar-search-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  padding: 0.5rem;
  font-size: 0.85rem;
  color: var(--text-primary);
  min-width: 0;
}
.sidebar-search-input::placeholder {
  color: var(--text-muted);
  font-size: 0.8rem;
}
.search-clear {
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  padding: 0.25rem;
  display: grid;
  place-items: center;
}
.search-clear:hover {
  color: var(--text-primary);
}

.filter-results {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
}
.filter-empty {
  color: var(--text-muted);
  font-size: 0.85rem;
  text-align: center;
  padding: 1rem 0.5rem;
  margin: 0;
}

.favorites-section {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  padding-top: 0.25rem;
  border-top: 1px dashed var(--border-subtle);
}
.section-label {
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  padding: var(--space-2) var(--space-3) 0;
}

.menu-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: 0.625rem;
  color: var(--text-secondary);
  text-decoration: none;
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.menu-item:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}

.menu-item.active {
  background: var(--interactive-primary-soft);
  color: var(--interactive-primary);
}

.menu-row {
  position: relative;
  display: flex;
  align-items: center;
  border-radius: 0.625rem;
}
.menu-row:hover {
  background: var(--surface-hover);
}
.menu-row.active {
  background: var(--interactive-primary-soft);
}
.menu-row .menu-item-link {
  flex: 1;
  min-width: 0;
}
.menu-row:hover .menu-item-link,
.menu-row.active .menu-item-link {
  background: transparent;
}
.fav-toggle {
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  padding: 0.25rem 0.5rem;
  margin-right: 0.25rem;
  display: grid;
  place-items: center;
  border-radius: 4px;
  font-size: 0.85rem;
  opacity: 0;
  transition: opacity var(--transition-fast), color var(--transition-fast);
}
.menu-row:hover .fav-toggle,
.fav-toggle.active,
.fav-toggle:focus-visible {
  opacity: 1;
}
.fav-toggle:hover {
  color: var(--text-primary);
}
.fav-toggle.active {
  color: var(--interactive-primary, #f5b400);
}

.sidebar-icons {
  display: grid;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-2);
  overflow: auto;
}

.icon-item {
  width: 2.5rem;
  height: 2.5rem;
  border-radius: 0.75rem;
  display: grid;
  place-items: center;
  color: var(--text-secondary);
  text-decoration: none;
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.icon-item:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}

.icon-item.active {
  background: var(--interactive-primary-soft);
  color: var(--interactive-primary);
}

:deep(.p-panelmenu .p-panelmenu-panel) {
  background: transparent;
  border: none;
}

:deep(.p-panelmenu .p-panelmenu-header-content) {
  background: transparent;
  border: none;
}

:deep(.p-panelmenu .p-panelmenu-header-link) {
  color: var(--text-secondary);
  font-size: 0.8125rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: var(--space-2) var(--space-3);
  background: transparent;
  font-weight: 600;
}
:deep(.p-panelmenu .p-panelmenu-header-link:hover) {
  color: var(--text-primary);
  background: var(--surface-hover);
}
:deep(.p-panelmenu .p-panelmenu-header-link .p-icon),
:deep(.p-panelmenu .p-panelmenu-header-link .pi) {
  color: var(--text-secondary);
}
:deep(.p-panelmenu .p-panelmenu-header-link[aria-expanded="true"]),
:deep(.p-panelmenu .p-panelmenu-header.p-highlight .p-panelmenu-header-link) {
  color: var(--text-primary);
}

.menu-group-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.menu-group-header i {
  font-size: 0.875rem;
  opacity: 0.7;
}

:deep(.p-panelmenu .p-panelmenu-content) {
  border: none;
  background: transparent;
  padding-bottom: var(--space-2);
}

.mobile-sidebar {
  border-right: none;
  box-shadow: var(--shadow-lg);
}

.sidebar-tour-replay {
  margin-top: auto;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: transparent;
  border: none;
  border-top: 1px solid var(--p-content-border-color);
  color: var(--text-primary);
  font-size: 0.875rem;
  cursor: pointer;
  text-align: left;
  width: 100%;
}
.sidebar-tour-replay:hover {
  background: var(--p-content-hover-background);
}
.sidebar-tour-replay.collapsed {
  justify-content: center;
  padding: var(--space-2);
}

.sidebar-build-stamp {
  padding: var(--space-2) var(--space-3);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.7rem;
  color: var(--p-text-muted-color);
  opacity: 0.6;
  text-align: center;
  border-top: 1px solid var(--p-content-border-color);
  user-select: text;
}
.app-sidebar.collapsed .sidebar-build-stamp {
  font-size: 0.6rem;
  padding: var(--space-2) var(--space-1);
}
</style>
