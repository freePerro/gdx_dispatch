/**
 * Bottom-nav / FAB clearance has ONE owner: AppLayout's .layout-content
 * (token --bottom-nav-clearance in base.css, applied only when the
 * quick-capture FAB renders). On 2026-08-31 twelve view roots that render
 * INSIDE that container also padded for the nav — the two stacked into
 * 170–244px of dead space. This is an absence guard: a view root that
 * grows a nav pad again fails here, whatever the file is called.
 *
 * Layout itself cannot be proven in jsdom (no media queries) — the browser
 * measurement in the PR is the proof of the geometry; this only keeps the
 * shape from coming back.
 */
import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const VIEWS = join(__dirname, '..', 'views');

// Inner scroll boxes (their own overflow-y:auto) legitimately pad for the nav
// because the layout's padding is outside their scroll area.
const ALLOWED = new Set(['PhoneComMessagesView.vue']);

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : p.endsWith('.vue') ? [p] : [];
  });
}

const NAV_PAD = /5rem\s*\+\s*env\(|--bottom-nav-(?:height|clearance)/;

describe('view roots do not pad for the bottom nav (AppLayout owns the clearance)', () => {
  it('no view under src/views pads by the nav height or the clearance token', () => {
    const offenders = walk(VIEWS)
      .filter((p) => !ALLOWED.has(p.split('/').pop()))
      .filter((p) => NAV_PAD.test(readFileSync(p, 'utf8')))
      .map((p) => p.slice(VIEWS.length + 1));
    expect(offenders).toEqual([]);
  });

  it('the allowlist only names files that actually exist', () => {
    const names = new Set(walk(VIEWS).map((p) => p.split('/').pop()));
    for (const a of ALLOWED) expect(names.has(a), a).toBe(true);
  });
});
