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

           Routes that need a bare shell (login, customer portal,
           full-screen onboarding wizard, the not-found fallback) opt out
           with `meta.noShell: true` and render directly into the bare
           <router-view>.

           KeyedRouterView is <router-view> plus one rule: the routed view
           REMOUNTS when navigation moves the same view to a different
           record (/estimates/A -> /estimates/B). Detail views load once in
           onMounted, so without it the old record stayed on screen while
           every action hit the new one. Query-only changes and a draft
           getting its own id keep the view mounted; see lib/viewRemount.js. -->
      <AppLayout v-if="!noShell">
        <KeyedRouterView />
      </AppLayout>
      <KeyedRouterView v-else />
    </ErrorBoundary>
    <CommandPalette v-model="commandPaletteOpen" />
  </ThemeProvider>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useToast } from 'primevue/usetoast';
import ThemeProvider from './components/ThemeProvider.vue';
import CommandPalette from './components/CommandPalette.vue';
import ErrorBoundary from './components/ErrorBoundary.vue';
import AppLayout from './components/AppLayout.vue';
import KeyedRouterView from './components/KeyedRouterView.vue';
import Toast from 'primevue/toast';
import { useIdleLogout } from './composables/useIdleLogout';

const commandPaletteOpen = ref(false);
const toast = useToast();
const route = useRoute();
const router = useRouter();

// Inactivity auto-logout (configured in Settings → Feature Settings).
useIdleLogout();

// Until the initial navigation resolves the route is START_LOCATION: it matches
// nothing and has no meta, so the shell (topbar polls, sidebar) mounted for
// the first tick of every cold load — signed out, that fired a 401 before the
// guard reached /login (GDXA-1023). The catch-all route means a resolved route
// always matches something, so "matches nothing" is exactly "not resolved yet".
// A first navigation that FAILS (a lazy chunk on a weak signal, once the
// one-shot reload in router.onError is spent) never resolves: fall back to the
// shell so the user can navigate away instead of facing a blank page. Its
// fetches are gated on auth, so a signed-out user still draws no 401.
const firstNavFailed = ref(false);
router?.isReady?.().catch(() => { firstNavFailed.value = true; });
const routeResolved = computed(() => firstNavFailed.value || (route?.matched?.length ?? 0) > 0);
const noShell = computed(() => !routeResolved.value || Boolean(route?.meta?.noShell));

// base.css locks body scrolling because AppLayout scrolls inside its own main
// panel. A no-shell page has no such panel, so without this the portal and the
// public proposal page clip everything below the fold (see base.css).
watch(noShell, (bare) => document.body.classList.toggle('no-shell', bare), { immediate: true });

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
