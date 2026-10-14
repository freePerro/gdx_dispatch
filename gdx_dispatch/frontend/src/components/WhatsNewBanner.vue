<template>
  <div
    v-if="release"
    class="whats-new-banner"
    role="status"
    data-testid="whats-new-banner"
  >
    <i class="pi pi-sparkles banner-icon" aria-hidden="true" />
    <span class="banner-text">
      Updated to <strong>v{{ release.version }}</strong><template v-if="release.title"> — {{ release.title }}</template>
    </span>
    <span class="banner-actions">
      <RouterLink
        :to="`/release-notes/${release.version}`"
        class="banner-link"
        data-testid="whats-new-banner-open"
        @click="dismiss"
      >See what's new</RouterLink>
      <button
        type="button"
        class="banner-close"
        aria-label="Dismiss"
        data-testid="whats-new-banner-dismiss"
        @click="dismiss"
      ><i class="pi pi-times" aria-hidden="true" /></button>
    </span>
  </div>
</template>

<script setup>
// Shown once per person after the app is updated: the installed release has
// notes and this user has no completed/skipped release-notes row at or above
// it (composables/useReleaseNotes.js). Dismissing, following the link or
// opening /release-notes all record it as seen.
import { onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import {
  hasSeenRelease,
  loadReleaseNotes,
  loadSeenProgress,
  markReleaseSeen,
} from '../composables/useReleaseNotes';
import { useAuthStore } from '../stores/auth';

const route = useRoute();
const auth = useAuthStore();
const release = ref(null);

// Opening the notes page some other way (sidebar, menu) reads them too.
watch(() => route.path, (path) => {
  if (release.value && path.startsWith('/release-notes')) release.value = null;
});

async function dismiss() {
  const version = release.value?.version;
  release.value = null;
  if (version) await markReleaseSeen(version);
}

onMounted(async () => {
  // Both reads are behind login. App.vue keeps the shell (and this banner)
  // off until the first navigation resolves; this holds if it mounts early
  // anyway (GDXA-1023).
  if (!auth.isAuthenticated) return;
  let data;
  try {
    data = await loadReleaseNotes();
  } catch {
    return;
  }
  const installed = data?.installed_version;
  const current = (data?.releases || []).find((r) => r.version === installed);
  if (!current) return;
  if (route.path.startsWith('/release-notes')) return;
  const progress = await loadSeenProgress();
  if (!hasSeenRelease(progress, installed)) release.value = current;
});
</script>

<style scoped>
.whats-new-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--p-highlight-background);
  color: var(--p-highlight-color);
  border-bottom: 1px solid var(--p-content-border-color);
  font-size: 0.9rem;
}
.banner-icon {
  flex: none;
}
.banner-text {
  flex: 1 1 12rem;
  min-width: 0;
  overflow-wrap: anywhere;
}
.banner-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-left: auto;
}
.banner-link {
  color: inherit;
  font-weight: 600;
  text-decoration: underline;
  white-space: nowrap;
}
.banner-close {
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: var(--space-1);
  min-width: 2rem;
  min-height: 2rem;
  border-radius: 4px;
}
.banner-close:hover {
  background: color-mix(in srgb, currentColor 12%, transparent);
}
</style>
