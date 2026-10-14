import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// PrimeVue's TabList schedules a 150ms updateInkBar setTimeout on mount; when a
// Tabs-using view unmounts before it fires (or at worker teardown), the callback
// runs against a torn-down jsdom and throws an UNHANDLED error AFTER the tests
// finish — failing the run on CI's slower timing even though every test passes
// (primefaces/primevue#7410). Only that timer is dropped, recognised by its
// TabList ink-bar frame in the stack. Everything else unhandled still fails the
// run: the blanket dangerouslyIgnoreUnhandledErrors this replaced hid 18 real
// `auth.loadPermissions is not a function` errors in the Dashboard specs
// (GDXA-1068).
const PRIMEVUE_INKBAR_FRAME = /\b(updateInkBar|bindInkBarObserver)\b/;

export function isPrimeVueInkBarTimerError(err) {
  const frames = [
    String(err?.stack ?? ''),
    ...(Array.isArray(err?.stacks) ? err.stacks.map((f) => `${f?.method ?? ''} ${f?.file ?? ''}`) : []),
  ].join('\n');
  return PRIMEVUE_INKBAR_FRAME.test(frames);
}

export default defineConfig({
  plugins: [vue()],
  resolve: {
    // Mirror vite.config.js — views import composables via `@/…`
    // (MobileTodayView.vue was the first one a test actually mounted).
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.js'],
    // Returning false drops the error from the run's error set (vitest 4.1).
    onUnhandledError: (err) => (isPrimeVueInkBarTimerError(err) ? false : undefined),
    // ux_gate.test.mjs is a node CLI script (run via `node scripts/ux_gate.test.mjs`),
    // not a vitest test — `import.meta.url` resolves to a Vite `/@fs/...`
    // path under vitest and breaks the gate's relative path resolution.
    exclude: ['e2e/**', 'node_modules/**', 'dist/**', 'scripts/**'],
  },
});
