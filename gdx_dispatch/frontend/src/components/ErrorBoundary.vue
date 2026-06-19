<!--
  ErrorBoundary — converts a thrown render/setup/mount error into a visible
  fallback message so a broken page never silently white-screens. The error
  itself is already reported through plugins/errorCapture.js (vue_error
  channel); this component owns the visible-to-user side.

  Usage: wrap <router-view> (or any subtree) in <ErrorBoundary>. On a thrown
  error, the slot is replaced with a fallback panel + Retry button. Retry
  clears the error and re-renders the slot — useful when the cause was a
  transient bad response or a missing-prop race that resolves on next
  navigation.

  Sprint reference: sprint-bleeding-errors P2a.1.
-->
<template>
  <div v-if="error" class="error-boundary-fallback">
    <h2>We hit an error rendering this page</h2>
    <p>The error has been reported. Try again — if it keeps happening, contact support.</p>
    <button type="button" class="retry-btn" @click="retry">Retry</button>
    <details v-if="showDetail">
      <summary>Details</summary>
      <pre>{{ error?.message || String(error) }}</pre>
    </details>
  </div>
  <slot v-else />
</template>

<script setup>
import { onErrorCaptured, ref } from 'vue';

const props = defineProps({
  // Show error message to user. Default off — most users don't need it.
  showDetail: { type: Boolean, default: false },
});

const error = ref(null);

onErrorCaptured((err) => {
  error.value = err;
  // Returning false stops propagation; we want the global errorHandler
  // (plugins/errorCapture.js) to still see it for telemetry, so return
  // true to allow continuation.
  return true;
});

function retry() {
  error.value = null;
}
</script>

<style scoped>
.error-boundary-fallback {
  max-width: 640px;
  margin: 4rem auto;
  padding: 2rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 8px;
  background: var(--p-content-background, #fff);
  text-align: center;
}
.error-boundary-fallback h2 {
  margin: 0 0 0.75rem;
  font-size: 1.25rem;
}
.error-boundary-fallback p {
  margin: 0 0 1.5rem;
  color: var(--p-text-muted-color, #6b7280);
}
.retry-btn {
  padding: 0.5rem 1.25rem;
  border-radius: 6px;
  border: 1px solid var(--p-primary-color, #3b82f6);
  background: var(--p-primary-color, #3b82f6);
  color: white;
  cursor: pointer;
}
.retry-btn:hover {
  opacity: 0.9;
}
.error-boundary-fallback details {
  margin-top: 1.5rem;
  text-align: left;
  font-size: 0.85rem;
}
.error-boundary-fallback pre {
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--p-content-hover-background, #f3f4f6);
  padding: 0.75rem;
  border-radius: 4px;
}
</style>
