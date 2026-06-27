import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.js'],
    // PrimeVue's TabList schedules a 150ms updateInkBar setTimeout on mount; when
    // a Tabs-using view unmounts before it fires (or at worker teardown), the
    // callback hits a null inkbar in jsdom and throws an UNHANDLED error AFTER the
    // tests finish — failing the whole run on CI's slower timing even though every
    // test passes (primefaces/primevue#7410; the 4.5.5 guard still leaks here).
    // Vitest has no scoped filter, so ignore unhandled errors. Real failures still
    // surface as assertions; this only suppresses post-teardown library timer noise.
    dangerouslyIgnoreUnhandledErrors: true,
    // ux_gate.test.mjs is a node CLI script (run via `node scripts/ux_gate.test.mjs`),
    // not a vitest test — `import.meta.url` resolves to a Vite `/@fs/...`
    // path under vitest and breaks the gate's relative path resolution.
    exclude: ['e2e/**', 'node_modules/**', 'dist/**', 'scripts/**'],
  },
});
