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
 * The map lives in lib/notificationDestinations.js (shared with the Android
 * office alerts since 2026-10-04), a plain function, so these call it. The
 * category list is read from its source so a new case is covered too.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { destinationFor } from '../../lib/notificationDestinations';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(__dirname, '../../lib/notificationDestinations.js'), 'utf8');
const CATEGORIES = [...SRC.matchAll(/case '([^']+)':/g)].map((m) => m[1]);

describe('NotificationsDrawer destinations', () => {
  it('sends inbox/message notifications to the real Outlook mailbox on desktop', () => {
    for (const category of ['inbox', 'message']) {
      expect(destinationFor(category), `${category} must not route to the in-memory Communications screen`)
        .toBe('/inbox');
      expect(destinationFor(category, { mobile: true })).toBe('/mobile/inbox');
    }
  });

  it('no destination anywhere in the map points at /communications', () => {
    expect(CATEGORIES.length).toBeGreaterThan(3);
    for (const category of CATEGORIES) {
      for (const mobile of [false, true]) {
        expect(destinationFor(category, { mobile }), `${category} still points at the fake screen`)
          .not.toBe('/communications');
      }
    }
  });
});
