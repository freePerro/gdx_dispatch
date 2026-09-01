/**
 * 2026-08-31 (#350): the Communications screen was removed. It was a third
 * messaging system next to the two that work — threads lived in a dict in
 * the API process, its senders were unconfigured, and Send answered with a
 * green toast when nothing left the building. Three paths must keep working
 * for old links and bookmarks by landing on the real mailbox (/inbox), and
 * no route or nav entry may quietly bring the dead view back.
 */
import { describe, expect, it } from 'vitest';
import { routes } from '../index.js';
import { MODULE_CATEGORIES, flattenModules } from '../../constants/modules.js';

function flatten(list) {
  return list.flatMap((r) => [r, ...(r.children ? flatten(r.children) : [])]);
}

describe('/communications after the screen was removed', () => {
  it.each(['/communications', '/messages', '/inbound-comms'])(
    '%s redirects to /inbox instead of rendering a component',
    (path) => {
      const hit = flatten(routes).filter((r) => r.path === path);
      expect(hit).toHaveLength(1);
      expect(hit[0].redirect).toBe('/inbox');
      expect(hit[0].component).toBeUndefined();
    },
  );

  it('still has a real /inbox route to land on', () => {
    const inbox = flatten(routes).find((r) => r.path === '/inbox');
    expect(inbox).toBeTruthy();
    expect(inbox.component).toBeTruthy();
  });

  it('has no nav entry pointing at the dead page', () => {
    const items = flattenModules(MODULE_CATEGORIES);
    expect(items.some((m) => m.to === '/communications')).toBe(false);
    expect(items.some((m) => m.key === 'communications')).toBe(false);
    // The two real channels are still reachable from the nav.
    expect(items.find((m) => m.to === '/inbox')).toBeTruthy();
    expect(items.find((m) => m.to === '/phone-com/messages')).toBeTruthy();
  });
});
