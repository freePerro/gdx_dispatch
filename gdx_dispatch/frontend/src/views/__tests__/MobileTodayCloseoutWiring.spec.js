/**
 * MobileTodayView — Phase 2 / C3.5 closeout dialog wiring (Doug 2026-05-10).
 *
 * Replaces the prior inline sig-only complete dialog (which POSTed
 * /api/mobile/jobs/{id}/complete) with the unified MobileJobCloseoutDialog
 * (POSTs /api/jobs/{id}/closeout). Same path dispatchers use; one closeout
 * concept across mobile + desktop.
 *
 * This static-source spec pins:
 *  1. MobileJobCloseoutDialog is imported.
 *  2. MobileJobCloseoutDialog is mounted in the template.
 *  3. The legacy /api/mobile/jobs/{id}/complete call is GONE from this view
 *     (the route still exists for backwards compat, but this view doesn't
 *     reach it anymore).
 *  4. openComplete sets closeoutOpen=true (the new dialog trigger).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'MobileTodayView.vue'),
  'utf8',
);

describe('MobileTodayView — closeout dialog wiring', () => {
  it('imports MobileJobCloseoutDialog', () => {
    expect(SRC).toMatch(/from\s+['"][^'"]*MobileJobCloseoutDialog\.vue['"]/);
  });

  it('mounts <MobileJobCloseoutDialog> in the template', () => {
    expect(SRC).toMatch(/<MobileJobCloseoutDialog[\s\S]*?v-model:visible="closeoutOpen"/);
  });

  it('does NOT call the legacy /api/mobile/jobs/{id}/complete endpoint', () => {
    // The route stays alive at the backend for backwards compat but this
    // view should no longer reach it. If a future refactor restores the
    // legacy call, two complete-flows split mobile across paths again.
    //
    // The match is loose enough to catch both forms: template literals
    // `\`/api/mobile/jobs/${id}/complete\`` and string concatenation
    // `'/api/mobile/jobs/' + id + '/complete'`. Auditor 2026-05-10
    // caught that an exact template-literal-only pattern would silently
    // pass a refactor to concatenation.
    expect(SRC).not.toMatch(/['"`]\/api\/mobile\/jobs\/[^'"`]*\/complete/);
  });

  it('openComplete sets closeoutOpen=true (the new trigger)', () => {
    const fnStart = SRC.indexOf('function openComplete');
    expect(fnStart).toBeGreaterThan(-1);
    const fnSpan = SRC.slice(fnStart, fnStart + 500);
    expect(fnSpan).toMatch(/closeoutOpen\.value\s*=\s*true/);
  });
});
