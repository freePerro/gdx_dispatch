/**
 * Architectural lint — App.vue owns the only <Toast> outlet.
 *
 * PrimeVue's ToastService is one global bus, and an outlet with no `group`
 * renders every message on it. A page that mounts its own <Toast> beside
 * App.vue's therefore shows each toast twice (GDXA-1780: "Time off settings
 * saved", "Time off requested"). The 14 page outlets were removed under
 * GDXA-1780's children; this test keeps them from coming back (GDXA-1790).
 *
 * Toast is not registered globally (main.js installs only ToastService), so a
 * component can only mount one by importing primevue/toast — the import check
 * catches render-function and `components:` uses the template check misses.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SRC_DIR = join(__dirname, '..');
const APP_VUE = join(SRC_DIR, 'App.vue');

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      yield* walk(full);
    } else if (name.endsWith('.vue')) {
      yield full;
    }
  }
}

const stripHtmlComments = (src) => src.replace(/<!--[\s\S]*?-->/g, '');
const TOAST_TAG = /<Toast[\s/>]/g;
const TOAST_IMPORT = /from\s+['"]primevue\/toast['"]/;

describe('architectural lint — <Toast> is mounted at App.vue, not per-page', () => {
  it('no .vue file other than App.vue uses a <Toast> tag', () => {
    const violations = [];
    for (const file of walk(SRC_DIR)) {
      if (file === APP_VUE) continue;
      if (TOAST_TAG.test(stripHtmlComments(readFileSync(file, 'utf8')))) {
        violations.push(file.replace(SRC_DIR + '/', ''));
      }
      TOAST_TAG.lastIndex = 0;
    }
    expect(violations).toEqual([]);
  });

  it('no .vue file other than App.vue imports primevue/toast', () => {
    const violations = [];
    for (const file of walk(SRC_DIR)) {
      if (file === APP_VUE) continue;
      if (TOAST_IMPORT.test(readFileSync(file, 'utf8'))) {
        violations.push(file.replace(SRC_DIR + '/', ''));
      }
    }
    expect(violations).toEqual([]);
  });

  it('App.vue mounts exactly one <Toast> outlet', () => {
    const matches = stripHtmlComments(readFileSync(APP_VUE, 'utf8')).match(TOAST_TAG) || [];
    expect(matches).toHaveLength(1);
  });
});
