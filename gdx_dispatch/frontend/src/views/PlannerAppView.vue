<template>
  <div class="planner-app" data-test="planner-app">
    <header class="pa-head">
      <span class="pa-title"><i class="pi pi-check-square" aria-hidden="true" /> GDX Planner</span>
      <Button
        icon="pi pi-ellipsis-v"
        text
        rounded
        aria-label="Menu"
        aria-haspopup="true"
        data-test="planner-app-menu"
        @click="menu.toggle($event)"
      />
      <Menu ref="menu" :model="menuItems" popup />
    </header>

    <!-- The planner app is the office's. A tech's phone has GDX Dispatch. -->
    <section v-if="isTech" class="pa-office-only" data-test="planner-app-office-only">
      <i class="pi pi-briefcase" aria-hidden="true" />
      <h1>GDX Planner is for office accounts</h1>
      <p>Your jobs, clock and notes are in the GDX Dispatch app.</p>
      <Button label="Sign out" icon="pi pi-sign-out" @click="signOut" />
    </section>

    <template v-else>
      <OfficeAlertsToggle planner class="pa-alerts" />
      <MobilePlannerView />
      <button
        type="button"
        class="pa-fab"
        aria-label="Quick note"
        data-test="planner-app-note"
        @click="captureOpen = true"
      >
        <i class="pi pi-pencil" aria-hidden="true" /> Note
      </button>
      <QuickCaptureSheet v-model:visible="captureOpen" @saved="onCaptureSaved" />
    </template>

    <MobileServerConfigDialog v-if="isNative" v-model:visible="serverOpen" />
  </div>
</template>

<script setup>
// GDX Planner — the whole of the planner-only Android app (lib/appMode.js):
// the planner, a quick note, phone alerts for reminders, and a menu with what
// the dispatch app's shell would otherwise provide (theme, server, sign out).
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Capacitor } from '@capacitor/core';
import Button from 'primevue/button';
import Menu from 'primevue/menu';
import MobilePlannerView from './MobilePlannerView.vue';
import OfficeAlertsToggle from '../components/OfficeAlertsToggle.vue';
import QuickCaptureSheet from '../components/QuickCaptureSheet.vue';
import MobileServerConfigDialog from '../components/MobileServerConfigDialog.vue';
import { useAuthStore } from '../stores/auth';
import { useThemeStore } from '../stores/theme';
import { isTechnician } from '../constants/roles';
import { useOfficeAlertsKeeper } from '../composables/useOfficeAlertsKeeper';

const router = useRouter();
const auth = useAuthStore();
const theme = useThemeStore();
const isNative = Capacitor.isNativePlatform();
// AppLayout keeps phone alerts matched to the signed-in user; this page has no
// AppLayout, so it does the same.
useOfficeAlertsKeeper();

const isTech = computed(() => isTechnician(auth.user?.role || auth.role));
const menu = ref(null);
const captureOpen = ref(false);
const serverOpen = ref(false);

const menuItems = computed(() => [
  {
    label: theme.effectiveMode === 'dark' ? 'Light mode' : 'Dark mode',
    icon: theme.effectiveMode === 'dark' ? 'pi pi-sun' : 'pi pi-moon',
    command: () => theme.toggleColorMode(),
  },
  ...(isNative
    ? [{ label: 'Server settings', icon: 'pi pi-server', command: () => { serverOpen.value = true; } }]
    : []),
  { separator: true },
  { label: 'Sign out', icon: 'pi pi-sign-out', command: signOut },
]);

function signOut() {
  auth.logout({ chosen: true });
  router.push('/login');
}

// The planner and its Today panel reload on this event.
function onCaptureSaved() {
  window.dispatchEvent(new CustomEvent('gdx:planner-refresh'));
}
</script>

<style scoped>
.planner-app {
  min-height: 100dvh;
  /* Clears the Note button, and the gesture bar under it. */
  padding-bottom: calc(5rem + env(safe-area-inset-bottom, 0px));
  background: var(--surface-app);
  color: var(--text-primary);
}

.pa-head {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: calc(0.5rem + env(safe-area-inset-top, 0px)) 0.75rem 0.5rem;
  background: var(--surface-header);
  border-bottom: 1px solid var(--border-subtle);
}

.pa-title {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 700;
  font-size: 1.05rem;
}

.pa-alerts {
  max-width: 800px;
  margin: 0 auto;
  padding: 0.25rem 0.75rem 0;
}

.pa-fab {
  position: fixed;
  right: 1rem;
  bottom: calc(1rem + env(safe-area-inset-bottom, 0px));
  z-index: 6;
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  min-height: 3rem;
  padding: 0 1.25rem;
  border: none;
  border-radius: 1.5rem;
  background: var(--p-primary-color);
  color: var(--text-on-p-primary);
  font: inherit;
  font-weight: 600;
  box-shadow: 0 4px 12px rgb(0 0 0 / 25%);
  cursor: pointer;
}

.pa-office-only {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.75rem;
  max-width: 28rem;
  margin: 3rem auto 0;
  padding: 0 1.5rem;
  text-align: center;
}

.pa-office-only .pi-briefcase {
  font-size: 2.5rem;
  color: var(--text-muted);
}

.pa-office-only h1 {
  margin: 0;
  font-size: 1.25rem;
}

.pa-office-only p {
  margin: 0;
  color: var(--text-muted);
}
</style>
