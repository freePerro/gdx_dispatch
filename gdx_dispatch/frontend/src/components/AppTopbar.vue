<template>
  <Toolbar class="app-topbar">
    <template #start>
      <div class="topbar-left">
        <!-- Hamburger hidden on mobile routes — bottom nav covers navigation
             on phone-shaped viewports. -->
        <Button
          v-if="!isOnMobileRoute"
          type="button"
          icon="pi pi-bars"
          class="p-button-text"
          aria-label="Open navigation"
          v-tooltip="'Open navigation'"
          @click="$emit('toggle-navigation')"
        />
        <div class="company-name">{{ branding.company_name || 'Operations Console' }}</div>
      </div>
    </template>

    <template #center>
      <!-- Global search is keyboard-driven (Ctrl+K) — hide its preview on
           phone routes where there's no keyboard. -->
      <div v-if="!isOnMobileRoute" class="search-wrap" @click="emitOpenSearch">
        <i class="pi pi-search" aria-hidden="true" />
        <InputText
          id="global-search"
          name="global-search"
          class="search-input"
          :model-value="''"
          readonly
          placeholder="Search jobs, customers, invoices... (Ctrl+K)"
          aria-label="Global search"
        />
      </div>
    </template>

    <template #end>
      <div class="topbar-right">
        <!-- Quick-create buttons. Hidden on phone viewports — phone users
             create from MobileDispatch (jobs) and the mobile estimates
             flow. The role/permission gate is enforced backend-side. -->
        <Button
          v-if="!isOnMobileRoute && canCreate"
          type="button"
          icon="pi pi-plus"
          label="Job"
          class="quick-create-btn"
          severity="primary"
          size="small"
          aria-label="New job"
          data-test="topbar-new-job"
          data-tour="topbar-new-job"
          @click="newJob"
        />
        <Button
          v-if="!isOnMobileRoute && canCreate"
          type="button"
          icon="pi pi-file-edit"
          label="Estimate"
          class="quick-create-btn"
          severity="secondary"
          size="small"
          aria-label="New estimate"
          data-test="topbar-new-estimate"
          data-tour="topbar-new-estimate"
          @click="newEstimate"
        />

        <!-- Mobile-launch button is for desktop preview — pointless when
             you're already on the mobile route. -->
        <Button
          v-if="!isOnMobileRoute"
          type="button"
          class="p-button-text mobile-launch-btn"
          :class="{ 'is-mobile-viewport': isMobileViewport }"
          :aria-label="isMobileViewport ? 'Open mobile field view' : 'Open mobile field view (preview)'"
          v-tooltip="`Mobile field view${isMobileViewport ? '' : ' (preview)'}`"
          data-test="mobile-launch"
          @click="goToMobile"
        >
          <i class="pi pi-mobile" aria-hidden="true" />
        </Button>

        <Button
          v-if="aiAssistantEnabled"
          type="button"
          class="p-button-text ai-assistant-btn"
          aria-label="Open AI Assistant"
          v-tooltip="'Open AI Assistant'"
          data-test="ai-assistant-launcher"
          @click="aiDialogVisible = true"
        >
          <i class="pi pi-sparkles" aria-hidden="true" />
        </Button>

        <Dialog
          v-model:visible="aiDialogVisible"
          modal
          dismissable-mask
          header="AI Assistant"
          :style="{ width: '40rem', maxWidth: '95vw' }"
          :pt="{ root: { 'data-test': 'ai-assistant-dialog' } }"
        >
          <AIAssistantPanel :show-heading="false" />
        </Dialog>

        <!-- Theme toggle hidden on mobile — system color-scheme handles it. -->
        <Button
          v-if="!isOnMobileRoute"
          type="button"
          class="p-button-text theme-toggle-btn"
          :aria-label="themeIcon === 'pi-sun' ? 'Switch to light mode' : 'Switch to dark mode'"
          v-tooltip="themeIcon === 'pi-sun' ? 'Switch to light mode' : 'Switch to dark mode'"
          @click="theme.toggleColorMode()"
        >
          <i :class="['pi', themeIcon]" aria-hidden="true" />
        </Button>

        <HelpButton />

        <!-- Mobile-only search button. The desktop search box that opens the
             Ctrl+K palette is hidden on phone routes and the shortcut is
             unreachable without a keyboard, so give the palette a tap target
             here so global search actually works on a phone. -->
        <Button
          v-if="isOnMobileRoute"
          type="button"
          class="p-button-text mobile-search-btn"
          aria-label="Search"
          data-testid="mobile-search-btn"
          @click="emitOpenSearch"
        >
          <i class="pi pi-search" aria-hidden="true" />
        </Button>

        <Button
          type="button"
          class="p-button-text notification-btn"
          :class="{ 'has-flash': notificationFlash }"
          aria-label="Notifications"
          v-tooltip="'Notifications'"
          @click="$emit('show-notifications')"
        >
          <i class="pi pi-bell" aria-hidden="true" />
          <Badge
            v-if="notificationCount > 0"
            :value="notificationCount"
            severity="danger"
            :class="{ 'badge-flash': notificationFlash }"
          />
        </Button>

        <Button type="button" class="p-button-text avatar-btn" @click="toggleUserMenu" aria-label="User menu" v-tooltip="'User menu'">
          <Avatar
            :label="avatarLabel"
            shape="circle"
            class="user-avatar"
          />
        </Button>
        <Menu ref="userMenuRef" :model="userMenuItems" popup />
      </div>
    </template>
  </Toolbar>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import { storeToRefs } from 'pinia';
import { useRouter } from 'vue-router';
import Toolbar from 'primevue/toolbar';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import Badge from 'primevue/badge';
import Avatar from 'primevue/avatar';
import Menu from 'primevue/menu';
import Dialog from 'primevue/dialog';
import AIAssistantPanel from './AIAssistantPanel.vue';
import HelpButton from './HelpButton.vue';
import { useAuthStore } from '../stores/auth';
import { useThemeStore } from '../stores/theme';
import { useNotificationsStore } from '../stores/notifications';
import { useTenantModules } from '../composables/useTenantModules';
import { useViewMode } from '../composables/useViewMode';
import { isTechnician, humanizeRole } from '../constants/roles';

const emit = defineEmits(['toggle-navigation', 'open-search', 'show-notifications']);

const router = useRouter();
// True when the user is on a phone-shaped viewport — drop the desktop
// chrome (hamburger, search preview, mobile-launch button, theme toggle)
// regardless of which route they're on. This is what catches a tech who
// lands on /jobs or /dashboard from a deep link on their phone.
const isOnMobileRoute = computed(() => Boolean(viewMode.isMobileViewport.value));
const auth = useAuthStore();
const theme = useThemeStore();

const notifications = useNotificationsStore();
const { branding } = storeToRefs(theme);
const userMenuRef = ref();
const aiDialogVisible = ref(false);

const viewMode = useViewMode();
const { isMobileViewport } = viewMode;

function goToMobile() {
  viewMode.resetPreference();
  router.push('/mobile');
}

// Quick-create buttons. Reuse the desktop create flows via the same
// query-string conventions JobsView and EstimateView already honor
// (Dashboard's "+ New Job" / "+ New Estimate" do the same thing).
function newJob() {
  router.push({ path: '/jobs', query: { new: '1' } });
}
function newEstimate() {
  router.push({ path: '/estimates/new' });
}

// Field techs don't create jobs or estimates from the topbar — keep the
// office strip clean. Anyone non-tech sees the buttons; backend role/
// permission gates the actual POST.
const canCreate = computed(() => !isTechnician(auth.user?.role));

const tenantModules = useTenantModules();
// AI Assistant is OPT-IN per-tenant (paid/optional feature) AND
// role-gated — field techs don't need an Assistant in their topbar.
// useTenantModules.isEnabled() defaults to true for unknown modules
// to be permissive about core features; that's wrong for llm, which
// must be explicitly granted before the button surfaces.
const aiAssistantEnabled = computed(() => {
  // Field techs don't need an Assistant in their topbar (variant-aware).
  if (isTechnician(auth.user?.role)) return false;
  // Read the explicit flag, not the optimistic fallback.
  return tenantModules.enabledModules.value.llm === true;
});

// Start polling for real notification count from the API
notifications.startPolling();

// Flash the bell + badge briefly when the count *increases* (i.e. a new
// notification arrived between polls — e.g. a public lead-form submission).
// Watch the count, fire the CSS class for 3s on each increment.
const notificationFlash = ref(false);
const _lastSeenCount = ref(null);
watch(
  () => notifications.unreadCount,
  (newCount) => {
    if (_lastSeenCount.value !== null && newCount > _lastSeenCount.value) {
      notificationFlash.value = true;
      setTimeout(() => { notificationFlash.value = false; }, 3000);
    }
    _lastSeenCount.value = newCount;
  },
  { immediate: true },
);

const themeIcon = computed(() => theme.effectiveMode === 'dark' ? 'pi-sun' : 'pi-moon');
const notificationCount = computed(() => notifications.badgeCount);
const avatarLabel = computed(() => {
  const fullName = auth.user?.name || branding.value.company_name;
  return (fullName || 'U').trim().slice(0, 1).toUpperCase();
});

const userMenuItems = computed(() => {
  const u = auth.user || {};
  const c = auth.claims || {};
  // Identity fallback ladder: persisted user → JWT claims (email/sub) →
  // generic "Signed in". The JWT step matters when the store rehydrates
  // from sessionStorage with only the token (the pre-2026-05 login
  // response didn't include user_payload, and refresh paths can drop it).
  const displayName =
    u.name || u.full_name || u.email || c.email
    || (c.sub ? `User ${String(c.sub).slice(-4)}` : null)
    || 'Signed in';
  // Second row: email if we have one separate from displayName, else
  // role · company. Lets the user confirm WHICH account they're on.
  const emailRow = (u.email || c.email) && (u.email || c.email) !== displayName
    ? (u.email || c.email)
    : null;
  const subline = [humanizeRole(u.role || c.role), branding.value?.company_name]
    .filter(Boolean)
    .join(' · ');
  return [
    {
      label: displayName,
      // No command — purely an info row. PrimeMenu styles the disabled
      // item subtly which is the visual cue we want.
      disabled: true,
      icon: 'pi pi-user',
    },
    ...(emailRow
      ? [{
          label: emailRow,
          disabled: true,
          icon: 'pi pi-envelope',
        }]
      : []),
    ...(subline
      ? [{
          label: subline,
          disabled: true,
          icon: 'pi pi-tag',
        }]
      : []),
    { separator: true },
    {
      label: 'Profile',
      icon: 'pi pi-id-card',
      command: () => router.push('/profile'),
    },
    // MH-3 (audit P1 #12): in-app theme switch. Pre-fix the only way
    // for a tech to get dark mode was their phone's OS-level pref —
    // there was no mobile-chrome affordance. Each item calls
    // `theme.setColorMode` which persists to localStorage.gdx_theme and
    // applies `data-theme=<mode>` on <html>. The chevron icon marks
    // the currently-active mode (effectiveMode resolves 'auto' to the
    // OS pref so the dot lands on the real applied mode).
    {
      label: 'Theme: System',
      icon: theme.colorMode === 'auto' ? 'pi pi-check' : 'pi pi-desktop',
      command: () => theme.setColorMode('auto'),
    },
    {
      label: 'Theme: Light',
      icon: theme.colorMode === 'light' ? 'pi pi-check' : 'pi pi-sun',
      command: () => theme.setColorMode('light'),
    },
    {
      label: 'Theme: Dark',
      icon: theme.colorMode === 'dark' ? 'pi pi-check' : 'pi pi-moon',
      command: () => theme.setColorMode('dark'),
    },
    { separator: true },
    {
      label: 'Logout',
      icon: 'pi pi-sign-out',
      command: () => {
        auth.logout();
        router.push('/login');
      },
    },
  ];
});

function emitOpenSearch() {
  emit('open-search');
}

function toggleUserMenu(event) {
  userMenuRef.value.toggle(event);
}
</script>

<style scoped>
.app-topbar {
  height: var(--topbar-height);
  border: none;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 0;
  background: var(--surface-header);
  padding-inline: var(--space-4);
  color: var(--text-primary);
}

/* Force visible icon color on every text-button in the topbar.
   PrimeVue v4 button-text inherits from its own theme tokens which
   were rendering icons invisible on our surface-header background. */
.app-topbar :deep(.p-button.p-button-text) {
  color: var(--text-primary);
  background: transparent;
}
.app-topbar :deep(.p-button.p-button-text:hover) {
  color: var(--text-primary);
  background: var(--surface-hover);
}
.app-topbar :deep(.p-button.p-button-text .p-button-icon),
.app-topbar :deep(.p-button.p-button-text .pi) {
  color: var(--text-primary);
}
.app-topbar :deep(.p-button.p-button-text .p-button-label) {
  color: var(--text-primary);
}

.topbar-left,
.topbar-right {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.company-name {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--text-primary);
}

.search-wrap {
  width: min(40vw, 34rem);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: 0.75rem;
  padding: 0 var(--space-3);
  background: var(--surface-elevated);
  cursor: pointer;
}

.search-wrap .pi-search {
  color: var(--text-muted);
}

.search-input {
  width: 100%;
  background: transparent;
  border: none;
  padding: var(--space-2) 0;
  color: var(--text-primary);
}
.search-input::placeholder {
  color: var(--text-muted);
  opacity: 1;
}
:deep(.search-input .p-inputtext) {
  color: var(--text-primary);
  background: transparent;
  border: none;
}

.search-input:focus {
  box-shadow: none;
}

.quick-create-btn {
  margin-right: var(--space-1);
}

@media (max-width: 1024px) {
  /* On a tablet-shaped viewport collapse the labels — the icons alone
     are enough next to the search box. */
  .quick-create-btn :deep(.p-button-label) {
    display: none;
  }
}

.mobile-launch-btn.is-mobile-viewport :deep(.pi) {
  color: var(--interactive-primary);
}

.notification-btn {
  position: relative;
}

.notification-btn :deep(.p-badge) {
  margin-left: calc(var(--space-1) * -1);
}

/* Flash animation when the unread count increases (new lead arrives etc.) */
.notification-btn.has-flash :deep(.pi-bell) {
  animation: bell-shake 0.6s ease-in-out 4;
}

.badge-flash {
  animation: badge-pulse 0.8s ease-in-out 3;
}

@keyframes bell-shake {
  0%, 100% { transform: rotate(0); }
  20%      { transform: rotate(-12deg); }
  40%      { transform: rotate(10deg); }
  60%      { transform: rotate(-6deg); }
  80%      { transform: rotate(4deg); }
}

@keyframes badge-pulse {
  0%, 100% { transform: scale(1);   box-shadow: 0 0 0 0 rgba(239,68,68,0.7); }
  50%      { transform: scale(1.4); box-shadow: 0 0 0 10px rgba(239,68,68,0); }
}

@media (prefers-reduced-motion: reduce) {
  .notification-btn.has-flash :deep(.pi-bell),
  .badge-flash {
    animation: none;
  }
}

.user-avatar {
  background: var(--interactive-primary-soft);
  color: var(--interactive-primary);
}

@media (max-width: 960px) {
  .search-wrap {
    display: none;
  }
}
</style>
