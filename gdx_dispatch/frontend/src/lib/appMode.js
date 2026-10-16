/**
 * appMode.js — which app this bundle is running as.
 *
 * Owner Domain: frontend-shell (see .claude/agents/frontend-shell.md)
 *
 * One `dist` ships in two Android apps: GDX Dispatch (`com.gdx.dispatch`) and
 * GDX Planner (`com.gdx.planner`), a planner-only app for the office. The
 * native application id decides, read once at startup before the router
 * runs. A browser, the PWA and GDX Dispatch are all 'dispatch'.
 */

import { Capacitor } from '@capacitor/core';
import { App } from '@capacitor/app';

export const PLANNER_APP_ID = 'com.gdx.planner';
export const PLANNER_APP_PATH = '/planner-app';

let _mode = 'dispatch';

export async function detectAppMode() {
  if (!Capacitor.isNativePlatform()) return _mode;
  try {
    const info = await App.getInfo();
    _mode = info?.id === PLANNER_APP_ID ? 'planner' : 'dispatch';
  } catch (err) {
    console.warn('Could not read the app id; running as GDX Dispatch:', err);
  }
  return _mode;
}

export function isPlannerApp() {
  return _mode === 'planner';
}

// For tests.
export function _setAppMode(mode) {
  _mode = mode;
}

// What the planner app may show besides itself: signing in, a reset link and
// the forced password change (which lives on /profile).
const PLANNER_APP_ALLOWED = new Set([
  PLANNER_APP_PATH,
  '/login',
  '/forgot-password',
  '/reset-password',
  '/profile',
]);
const PLANNER_PATHS = new Set(['/planner', '/mobile/planner']);

/**
 * The router guard's answer in the planner app: true to let `to` through,
 * else where to go instead. A planner link (a tapped reminder opens
 * /mobile/planner?tab=tasks) keeps its query; anything else lands on the
 * planner's front page.
 */
export function plannerAppRoute(to) {
  if (PLANNER_APP_ALLOWED.has(to.path)) return true;
  if (PLANNER_PATHS.has(to.path)) return { path: PLANNER_APP_PATH, query: to.query };
  return { path: PLANNER_APP_PATH };
}
