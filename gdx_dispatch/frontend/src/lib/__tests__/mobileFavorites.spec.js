/**
 * mobileFavorites — the Android long-press shortcuts that mirror the More
 * drawer's favorites. The plugin is mocked here, so this proves what we ASK
 * the launcher for; that the launcher shows it is proven on a phone.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { native, set, clear, addListener } = vi.hoisted(() => ({
  native: { value: true },
  set: vi.fn().mockResolvedValue(undefined),
  clear: vi.fn().mockResolvedValue(undefined),
  addListener: vi.fn().mockResolvedValue({ remove: vi.fn() }),
}));

vi.mock('@capacitor/core', () => ({
  Capacitor: {
    isNativePlatform: () => native.value,
    getPlatform: () => (native.value ? 'android' : 'web'),
  },
}));
vi.mock('@capawesome/capacitor-app-shortcuts', () => ({
  AppShortcuts: { set, clear, addListener },
}));

// eslint-disable-next-line import/first
import {
  _resetForTests,
  installAppShortcutsBridge,
  MAX_SHORTCUTS,
  syncAppShortcuts,
} from '../mobileFavorites';

const entry = (n) => ({ to: `/m${n}`, label: `Module ${n}` });

describe('syncAppShortcuts', () => {
  beforeEach(() => {
    native.value = true;
    set.mockClear();
    clear.mockClear();
    addListener.mockClear();
    _resetForTests();
  });

  it('sends the first four favorites, route as id, with the star icon', async () => {
    await syncAppShortcuts([1, 2, 3, 4, 5, 6].map(entry));
    expect(set).toHaveBeenCalledTimes(1);
    const { shortcuts } = set.mock.calls[0][0];
    expect(shortcuts).toHaveLength(MAX_SHORTCUTS);
    expect(shortcuts[0]).toEqual({
      id: '/m1', title: 'Module 1', description: 'Module 1', androidIcon: 'ic_shortcut_star',
    });
    expect(shortcuts.map((s) => s.id)).toEqual(['/m1', '/m2', '/m3', '/m4']);
  });

  it('does not call the plugin again for an unchanged list', async () => {
    await syncAppShortcuts([entry(1)]);
    await syncAppShortcuts([entry(1)]);
    expect(set).toHaveBeenCalledTimes(1);
  });

  it('clears the shortcuts when the last favorite is removed', async () => {
    await syncAppShortcuts([entry(1)]);
    await syncAppShortcuts([]);
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it('a plugin failure is swallowed and retried next time', async () => {
    set.mockRejectedValueOnce(new Error('launcher said no'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await expect(syncAppShortcuts([entry(1)])).resolves.toBeUndefined();
    await syncAppShortcuts([entry(1)]);
    expect(set).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });

  it('does nothing off native', async () => {
    native.value = false;
    await syncAppShortcuts([entry(1)]);
    installAppShortcutsBridge({ push: vi.fn() });
    expect(set).not.toHaveBeenCalled();
    expect(addListener).not.toHaveBeenCalled();
  });
});

describe('installAppShortcutsBridge', () => {
  beforeEach(() => {
    native.value = true;
    addListener.mockClear();
    _resetForTests();
  });

  it('routes a tapped shortcut to its path, and ignores anything not an app path', async () => {
    const router = { push: vi.fn(), isReady: vi.fn().mockResolvedValue(undefined) };
    installAppShortcutsBridge(router);
    const onClick = addListener.mock.calls[0][1];
    await onClick({ shortcutId: '/mobile/sms' });
    await onClick({ shortcutId: 'https://evil.example' });
    await onClick({ shortcutId: '//evil.example' });
    expect(router.push).toHaveBeenCalledTimes(1);
    expect(router.push).toHaveBeenCalledWith('/mobile/sms');
  });

  it('waits for the first navigation before pushing (tap from a closed app)', async () => {
    let ready;
    const router = { push: vi.fn(), isReady: () => new Promise((r) => { ready = r; }) };
    installAppShortcutsBridge(router);
    const done = addListener.mock.calls[0][1]({ shortcutId: '/tasks' });
    await Promise.resolve();
    expect(router.push).not.toHaveBeenCalled();
    ready();
    await done;
    expect(router.push).toHaveBeenCalledWith('/tasks');
  });

  it('installs once', () => {
    installAppShortcutsBridge({ push: vi.fn() });
    installAppShortcutsBridge({ push: vi.fn() });
    expect(addListener).toHaveBeenCalledTimes(1);
  });
});
