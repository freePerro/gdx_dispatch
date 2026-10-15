/**
 * serverConfig — only the Android app reroutes API calls (audit 2026-10-14).
 *
 * The web app is served by the server it talks to. initServerConfig() used to
 * install the fetch interceptor everywhere, so a desktop user who saved an
 * address from the login gear sent /auth/login — password included — to it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({ on: false }));
vi.mock('@capacitor/core', () => ({
  Capacitor: { isNativePlatform: () => native.on, getPlatform: () => (native.on ? 'android' : 'web') },
}));
vi.mock('@capacitor/preferences', () => ({
  Preferences: { get: async () => ({ value: null }), set: async () => {}, remove: async () => {} },
}));

const realFetch = window.fetch;

describe('initServerConfig', () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
    localStorage.setItem('gdx_server_url', 'https://other.example');
    delete window.__gdx_fetch_intercepted__;
    window.fetch = vi.fn(async () => new Response('{}'));
  });
  afterEach(() => {
    window.fetch = realFetch;
    delete window.__gdx_fetch_intercepted__;
  });

  it('leaves fetch alone in the web app, whatever is saved', async () => {
    native.on = false;
    const stub = window.fetch;
    const { initServerConfig } = await import('../serverConfig');
    expect(await initServerConfig()).toBe('');
    await window.fetch('/auth/login');
    expect(window.fetch).toBe(stub);
    expect(stub).toHaveBeenCalledWith('/auth/login');
  });

  it('reroutes API calls to the saved server in the Android app', async () => {
    native.on = true;
    const stub = window.fetch;
    const { initServerConfig } = await import('../serverConfig');
    expect(await initServerConfig()).toBe('https://other.example');
    await window.fetch('/auth/login');
    expect(stub.mock.calls[0][0]).toBe('https://other.example/auth/login');
  });
});
