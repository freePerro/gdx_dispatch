/**
 * serverConfig — VITE_DEFAULT_SERVER_URL, the build-time server (GDXA-1993).
 *
 * A shop building its own APK bakes its server in; the tech's saved choice
 * still wins, and an unset variable changes nothing. The web app never
 * reroutes, whatever the build says.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({ on: true, pref: null }));
vi.mock('@capacitor/core', () => ({
  Capacitor: { isNativePlatform: () => native.on, getPlatform: () => (native.on ? 'android' : 'web') },
}));
vi.mock('@capacitor/preferences', () => ({
  Preferences: {
    get: async () => ({ value: native.pref }),
    set: async () => {},
    remove: async () => {},
  },
}));

const SHOP = 'https://dispatch.shop.example';
const realFetch = window.fetch;

async function load() {
  vi.resetModules();
  return import('../serverConfig');
}

describe('VITE_DEFAULT_SERVER_URL', () => {
  beforeEach(() => {
    localStorage.clear();
    native.on = true;
    native.pref = null;
    delete window.__gdx_fetch_intercepted__;
    window.fetch = vi.fn(async () => new Response('{}'));
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    window.fetch = realFetch;
    delete window.__gdx_fetch_intercepted__;
  });

  it('when set: lists it first as "This shop" and uses it with nothing stored', async () => {
    vi.stubEnv('VITE_DEFAULT_SERVER_URL', `${SHOP}/`);
    const stub = window.fetch;
    const cfg = await load();
    expect(cfg.PRESETS[0]).toEqual({ label: 'This shop', url: SHOP });
    expect(cfg.PRESETS.map((p) => p.label)).toEqual(['This shop', 'Public Demo', 'Local Dev (Emulator)']);
    expect(cfg.getServerUrl()).toBe(SHOP);
    expect(await cfg.initServerConfig()).toBe(SHOP);
    await window.fetch('/auth/login');
    expect(stub.mock.calls[0][0]).toBe(`${SHOP}/auth/login`);
  });

  it('when set: a choice saved in Preferences wins', async () => {
    vi.stubEnv('VITE_DEFAULT_SERVER_URL', SHOP);
    native.pref = 'https://chosen.example';
    const cfg = await load();
    expect(await cfg.initServerConfig()).toBe('https://chosen.example');
    expect(cfg.getServerUrl()).toBe('https://chosen.example');
  });

  it('when set: a choice saved in localStorage wins', async () => {
    vi.stubEnv('VITE_DEFAULT_SERVER_URL', SHOP);
    localStorage.setItem('gdx_server_url', 'https://chosen.example');
    const cfg = await load();
    expect(cfg.getServerUrl()).toBe('https://chosen.example');
    expect(await cfg.initServerConfig()).toBe('https://chosen.example');
  });

  it('when set: clearing the saved choice falls back to the shop default', async () => {
    vi.stubEnv('VITE_DEFAULT_SERVER_URL', SHOP);
    const cfg = await load();
    await cfg.setServerUrl('https://chosen.example');
    expect(cfg.getServerUrl()).toBe('https://chosen.example');
    await cfg.setServerUrl('');
    expect(cfg.getServerUrl()).toBe(SHOP);
  });

  it('when set: the web app still never reroutes', async () => {
    vi.stubEnv('VITE_DEFAULT_SERVER_URL', SHOP);
    native.on = false;
    const stub = window.fetch;
    const cfg = await load();
    expect(cfg.getServerUrl()).toBe('');
    expect(await cfg.initServerConfig()).toBe('');
    expect(cfg.resolveApiUrl('/api/jobs')).toBe('/api/jobs');
    expect(window.fetch).toBe(stub);
  });

  it.each([['unset', undefined], ['empty', ''], ['blank', '   ']])(
    'when %s: presets and the initial URL are unchanged',
    async (_name, value) => {
      if (value !== undefined) vi.stubEnv('VITE_DEFAULT_SERVER_URL', value);
      const cfg = await load();
      expect(cfg.DEFAULT_SERVER_URL).toBe('');
      expect(cfg.PRESETS.map((p) => p.label)).toEqual(['Public Demo', 'Local Dev (Emulator)']);
      expect(cfg.getServerUrl()).toBe('');
      expect(await cfg.initServerConfig()).toBe('');
    },
  );
});
