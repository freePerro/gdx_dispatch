<template>
  <ThemeProvider>
    <Toast position="bottom-right" />
    <ErrorBoundary>
      <!-- AppLayout is mounted ONCE here at the App.vue root, with the
           per-route view rendered into its <slot> via <router-view>. This
           gives a stable component tree across navigations: the sidebar +
           topbar + bottom-nav never unmount, only the slot content swaps.
           That kills the same-root-component-swap class of bug (Vue could
           leave a previous view's section orphaned in <main> when two
           consecutive views shared the same template root — caught
           2026-05-09 via /maps and worked around with a hard-reload guard
           + :key="$route.fullPath" on <router-view>; both removed in
           the AppLayout-into-App.vue refactor).

           Routes that need a bare shell (login, signup, customer portal,
           full-screen onboarding wizard, the not-found fallback) opt out
           with `meta.noShell: true` and render directly into the bare
           <router-view>. -->
      <AppLayout v-if="!noShell">
        <router-view />
      </AppLayout>
      <router-view v-else />
    </ErrorBoundary>
    <CommandPalette v-model="commandPaletteOpen" />
  </ThemeProvider>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { useToast } from 'primevue/usetoast';
import ThemeProvider from './components/ThemeProvider.vue';
import CommandPalette from './components/CommandPalette.vue';
import ErrorBoundary from './components/ErrorBoundary.vue';
import AppLayout from './components/AppLayout.vue';
import Toast from 'primevue/toast';
import { useIdleLogout } from './composables/useIdleLogout';

const commandPaletteOpen = ref(false);
const toast = useToast();
const route = useRoute();

// Inactivity auto-logout (configured in Settings → Feature Settings).
useIdleLogout();

const noShell = computed(() => Boolean(route?.meta?.noShell));

// Dedupe toasts for repeated identical errors inside a short window — the
// user only needs to know once per cluster.
const recentToasts = new Map();
function handleRuntimeError(event) {
  const detail = event?.detail || {};
  const key = `${detail.kind}:${detail.message || ''}`;
  const now = Date.now();
  const last = recentToasts.get(key) || 0;
  if (now - last < 8000) return;
  recentToasts.set(key, now);
  toast.add({
    severity: 'error',
    summary: 'Something went wrong',
    detail: detail.message || 'An unexpected error occurred. Try again — if it persists, reload.',
    life: 5000,
  });
}

function handleGlobalShortcut(event) {
  const isOpenPaletteKey = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k';

  if (isOpenPaletteKey) {
    event.preventDefault();
    commandPaletteOpen.value = true;
  }

  if (event.key === 'Escape' && commandPaletteOpen.value) {
    commandPaletteOpen.value = false;
  }
}

function openFromEvent() {
  commandPaletteOpen.value = true;
}

onMounted(() => {
  window.addEventListener('keydown', handleGlobalShortcut);
  window.addEventListener('gdx:open-command-palette', openFromEvent);
  window.addEventListener('gdx-runtime-error', handleRuntimeError);
});

onUnmounted(() => {
  window.removeEventListener('keydown', handleGlobalShortcut);
  window.removeEventListener('gdx:open-command-palette', openFromEvent);
  window.removeEventListener('gdx-runtime-error', handleRuntimeError);
});
</script>
