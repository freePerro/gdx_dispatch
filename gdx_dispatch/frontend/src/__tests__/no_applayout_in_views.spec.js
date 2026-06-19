/**
 * Architectural lint — no view file may wrap its template in <AppLayout>.
 *
 * After the AppLayout-into-App.vue refactor, AppLayout is mounted exactly
 * once at App.vue with <router-view> as its slot. A view that re-wraps in
 * <AppLayout> would mount the shell twice (double sidebar, double topbar)
 * AND re-introduce the same-root-component-swap bug class (orphaned
 * sections in <main> on navigation between views that share AppLayout
 * roots). This test fails loudly if any future commit slips one back in.
 *
 * Excluded: __tests__/ stubs (allowed — they're harmless mocks),
 *          AppLayout.vue itself, _ViewTemplate.vue (template comments only).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const VIEWS_DIR = join(__dirname, '..', 'views');

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name === '__tests__') continue;
      yield* walk(full);
    } else if (name.endsWith('.vue')) {
      yield full;
    }
  }
}

describe('architectural lint — <AppLayout> is mounted at App.vue, not per-view', () => {
  it('no view file uses <AppLayout> as a template tag', () => {
    const violations = [];
    for (const file of walk(VIEWS_DIR)) {
      const src = readFileSync(file, 'utf8');
      // Match opening tag form: <AppLayout> or <AppLayout ...> or <AppLayout/>
      // Skip comments — JS comments and HTML comments don't count.
      // Strip HTML comments before searching.
      const stripped = src.replace(/<!--[\s\S]*?-->/g, '');
      if (/<AppLayout[\s/>]/.test(stripped)) {
        violations.push(file.replace(VIEWS_DIR + '/', ''));
      }
    }
    expect(violations).toEqual([]);
  });

  it('no view file imports AppLayout (the shell lives in App.vue)', () => {
    const violations = [];
    for (const file of walk(VIEWS_DIR)) {
      const src = readFileSync(file, 'utf8');
      if (/import\s+AppLayout\s+from\s+['"][^'"]+AppLayout(?:\.vue)?['"]/.test(src)) {
        violations.push(file.replace(VIEWS_DIR + '/', ''));
      }
    }
    expect(violations).toEqual([]);
  });
});
