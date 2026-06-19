import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.js'],
    // ux_gate.test.mjs is a node CLI script (run via `node scripts/ux_gate.test.mjs`),
    // not a vitest test — `import.meta.url` resolves to a Vite `/@fs/...`
    // path under vitest and breaks the gate's relative path resolution.
    exclude: ['e2e/**', 'node_modules/**', 'dist/**', 'scripts/**'],
  },
});
