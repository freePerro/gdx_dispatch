/**
 * The closeout path has exactly one owner, and the legacy endpoint stays dead.
 *
 * Replaces MobileTodayCloseoutWiring.spec.js. That spec pinned closeout to
 * MobileTodayView by reading the .vue file as TEXT and asserting the import and
 * the mount were present — which proves authorship, not behaviour. PR B moved
 * every job action onto the detail screen, so those three assertions failed for
 * the right reason and the fourth became worse than useless:
 *
 *   "the legacy /api/mobile/jobs/{id}/complete call is GONE from this view"
 *
 * ...passes trivially on a view with no closeout code left at all. The July plan
 * predicted exactly this ("the guard silently dies either way") and made
 * re-pointing it an obligation of whichever PR gutted the view. This is that
 * re-pointing.
 *
 * The absence assertion now runs against the file that OWNS closeout, and it is
 * anchored: we first prove the file really does contain the closeout flow, so a
 * future refactor that moves closeout elsewhere fails loudly here instead of
 * going quietly green on an empty file.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const DETAIL = readFileSync(join(__dirname, '..', 'MobileJobDetailView.vue'), 'utf8');
const TODAY = readFileSync(join(__dirname, '..', 'MobileTodayView.vue'), 'utf8');

describe('closeout ownership', () => {
  it('the detail screen is where closeout actually lives', () => {
    // The anchor. Without this, every absence assertion below is vacuous.
    expect(DETAIL).toMatch(/from\s+['"][^'"]*MobileJobCloseoutDialog\.vue['"]/);
    expect(DETAIL).toMatch(/<MobileJobCloseoutDialog/);
    expect(DETAIL).toMatch(/closeoutOpen/);
  });

  it('does NOT reach the legacy /api/mobile/jobs/{id}/complete endpoint', () => {
    // The route still exists for back-compat; nothing in the app may call it.
    // Two spellings, per the 2026-05-10 auditor: template literal and string
    // concatenation.
    expect(DETAIL).not.toMatch(/['"`]\/api\/mobile\/jobs\/[^'"`]*\/complete/);
    expect(DETAIL).not.toMatch(/'\/api\/mobile\/jobs\/'\s*\+/);
  });

  it('Today no longer owns closeout, so it cannot drift back', () => {
    // Not an anchor-free absence: Today is asserted to have shed the whole
    // action surface, which is what PR B did, and to still be a real screen.
    expect(TODAY).toMatch(/MobileJobCard/);            // it still renders jobs
    expect(TODAY).not.toMatch(/MobileJobCloseoutDialog/);
    expect(TODAY).not.toMatch(/['"`]\/api\/mobile\/jobs\/[^'"`]*\/complete/);
  });

  it('one closeout concept: the detail screen posts the shared closeout path', () => {
    // MobileJobCloseoutDialog owns the POST to /api/jobs/{id}/closeout — the
    // same path dispatch uses. Pinned by the dialog being the only trigger.
    expect(DETAIL).toMatch(/closeoutOpen\s*=\s*true/);
  });
});
