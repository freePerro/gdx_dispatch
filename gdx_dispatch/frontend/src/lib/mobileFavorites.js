/**
 * mobileFavorites.js — starred modules in the phone's More drawer, and the
 * Android app's long-press shortcuts that mirror them.
 *
 * Owner Domain: mobile-tech (see .claude/agents/mobile-tech.md)
 *
 * Favorites are per device, in localStorage, like the desktop sidebar's
 * (AppSidebar.vue, `gdx_sidebar_favorites`). A separate key because the
 * drawer stores the phone-shaped route (`/mobile/sms`), not the desktop one.
 * A favorite whose module the user can no longer see is not deleted, just not
 * shown — the caller intersects with the permission-filtered module list.
 *
 * On the native Android app the first MAX_SHORTCUTS favorites become the
 * launcher's long-press shortcuts (@capawesome/capacitor-app-shortcuts);
 * every export that touches the plugin is a no-op off native.
 */

import { ref } from 'vue';
import { Capacitor } from '@capacitor/core';
import { AppShortcuts } from '@capawesome/capacitor-app-shortcuts';

export const FAVORITES_KEY = 'gdx_mobile_favorites';
// Android launchers show four app shortcuts in the long-press menu.
export const MAX_SHORTCUTS = 4;
// res/drawable/ic_shortcut_star.xml in the Android project.
const SHORTCUT_ICON = 'ic_shortcut_star';

function _load() {
  let raw;
  try { raw = localStorage.getItem(FAVORITES_KEY); } catch { raw = null; }
  try {
    const parsed = JSON.parse(raw || '[]');
    return Array.isArray(parsed) ? parsed.filter((p) => typeof p === 'string' && p.startsWith('/')) : [];
  } catch {
    return [];
  }
}

// One list for the whole app, so the drawer and the shortcut sync agree.
const favorites = ref(_load());

function _save() {
  try { localStorage.setItem(FAVORITES_KEY, JSON.stringify(favorites.value)); } catch { /* quota / private */ }
}

export function useMobileFavorites() {
  function isFavorite(to) {
    return favorites.value.includes(to);
  }
  function toggleFavorite(to) {
    if (!to || !to.startsWith('/')) return;
    favorites.value = isFavorite(to)
      ? favorites.value.filter((p) => p !== to)
      : [...favorites.value, to];
    _save();
  }
  return { favorites, isFavorite, toggleFavorite };
}

export function isAppShortcutsAvailable() {
  return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'android';
}

let _lastSynced = null;

/**
 * Make the launcher's long-press shortcuts the first MAX_SHORTCUTS of
 * `entries` ({ to, label }, already in favorites order and visible to this
 * user). Skips the plugin call when nothing changed. Never throws: a launcher
 * that refuses shortcuts must not break the drawer.
 */
export async function syncAppShortcuts(entries) {
  if (!isAppShortcutsAvailable()) return;
  const shortcuts = entries.slice(0, MAX_SHORTCUTS).map((e) => ({
    id: e.to,
    title: e.label,
    description: e.label,
    androidIcon: SHORTCUT_ICON,
  }));
  const key = JSON.stringify(shortcuts);
  if (key === _lastSynced) return;
  try {
    if (shortcuts.length) await AppShortcuts.set({ shortcuts });
    else await AppShortcuts.clear();
    _lastSynced = key;
  } catch (err) {
    console.warn('App shortcuts sync failed:', err);
  }
}

let _bridgeInstalled = false;

/**
 * Route a tapped long-press shortcut. The shortcut id is the route path.
 * From a closed app the click arrives before the first navigation settles,
 * and that navigation would land on top of ours, so wait for the router.
 */
export function installAppShortcutsBridge(router) {
  if (_bridgeInstalled || !isAppShortcutsAvailable()) return;
  _bridgeInstalled = true;
  AppShortcuts.addListener('click', async ({ shortcutId }) => {
    if (typeof shortcutId === 'string' && shortcutId.startsWith('/') && !shortcutId.startsWith('//')) {
      await router.isReady();
      router.push(shortcutId);
    }
  }).catch((err) => console.warn('App shortcuts listener failed:', err));
}

/** Test seam: re-read storage and forget the last sync. */
export function _resetForTests() {
  favorites.value = _load();
  _lastSynced = null;
  _bridgeInstalled = false;
}
