/**
 * A notification must land you on the screen that can actually hold the thing.
 *
 * `inbox` / `message` used to send desktop users to `/communications`, whose
 * store was a module-level dict in the API process
 * (`routers/communications.py` `_EMAILS_BY_TENANT`) that emptied on every
 * container restart (#492). The real Outlook-synced mailbox is `/inbox`
 * (`InboxView` -> `/api/outlook/*`). Mobile was already correct. The screen
 * and its router were removed outright in #350; this guard stays so nothing
 * quietly points a notification back at a path that is now a redirect.
 *
 * Source-level assertion, same pattern as OutlookSettingsAutoEmailNotice.spec.js:
 * the destination map is a plain switch inside a component that needs a store,
 * a router and PrimeVue to mount, and none of that is the thing under test.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(__dirname, '../NotificationsDrawer.vue'), 'utf8');

/** The `return` line of the case block that handles `category`. */
function destinationLineFor(category) {
  const at = SRC.indexOf(`case '${category}':`);
  expect(at, `no case block for '${category}'`).toBeGreaterThan(-1);
  const after = SRC.slice(at);
  const line = after.split('\n').find((l) => l.includes('return mobile ?'));
  expect(line, `no destination return after case '${category}'`).toBeTruthy();
  return line;
}

describe('NotificationsDrawer destinations', () => {
  it('sends inbox/message notifications to the real Outlook mailbox on desktop', () => {
    for (const category of ['inbox', 'message']) {
      const line = destinationLineFor(category);
      expect(line, `${category} must not route to the in-memory Communications screen`)
        .not.toMatch(/:\s*'\/communications'/);
      expect(line).toMatch(/:\s*'\/inbox'/);
      expect(line).toMatch(/'\/mobile\/inbox'/);
    }
  });

  it('no destination anywhere in the map points at /communications', () => {
    const returns = SRC.split('\n').filter((l) => l.includes('return mobile ?'));
    expect(returns.length).toBeGreaterThan(3);
    for (const line of returns) {
      expect(line, `destination still points at the fake screen: ${line.trim()}`)
        .not.toMatch(/'\/communications'/);
    }
  });
});
