/**
 * serverConfig.js — Persistent Server URL management for GDX Dispatch Mobile & Web.
 *
 * Owner Domain: mobile-tech (see .claude/agents/mobile-tech.md)
 *
 * Purpose:
 * GDX Dispatch is a single-tenant, self-hosted field-service management platform.
 * On standard desktop / web deployments, all requests use relative URLs ('/api/...',
 * '/auth/...', etc.) against the serving origin.
 *
 * When packaged as an installable Android app (via Capacitor), the web application
 * is served from local device storage ('https://localhost/'). To communicate with
 * the backend:
 * 1. Technicians can configure the server instance URL (e.g. 'https://dispatch.company.com'
 *    or 'http://10.0.2.2:8001' in local testing).
 * 2. The URL is saved persistently in @capacitor/preferences (with localStorage fallback).
 * 3. An ambient fetch interceptor resolves relative API/auth/portal endpoints to the
 *    configured server URL while leaving local bundled assets (e.g. help-index.json)
 *    on https://localhost/.
 */

import { Capacitor } from '@capacitor/core';
import { Preferences } from '@capacitor/preferences';

export const SERVER_STORAGE_KEY = 'gdx_server_url';

// Default presets for quick configuration in dialog
export const PRESETS = [
  { label: 'Public Demo', url: 'https://gdxdispatch.com' },
  { label: 'Local Dev (Emulator)', url: 'http://10.0.2.2:8001' },
];

let _cachedServerUrl = '';
let _initialized = false;

/**
 * Normalizes user-entered server URLs:
 * - Trims whitespace
 * - Strips trailing slashes
 * - Defaults protocol to https:// if omitted
 */
export function cleanServerUrl(url) {
  if (!url || typeof url !== 'string') return '';
  let cleaned = url.trim();
  cleaned = cleaned.replace(/\/+$/, '');
  if (!cleaned) return '';
  if (!cleaned.startsWith('http://') && !cleaned.startsWith('https://')) {
    cleaned = `https://${cleaned}`;
  }
  return cleaned;
}

/**
 * Returns current server URL (cached in memory for synchronous fetch interception).
 */
export function getServerUrl() {
  if (!_cachedServerUrl && typeof localStorage !== 'undefined') {
    _cachedServerUrl = cleanServerUrl(localStorage.getItem(SERVER_STORAGE_KEY) || '');
  }
  return _cachedServerUrl;
}

/**
 * Sets and persists server URL across both @capacitor/preferences and localStorage.
 */
export async function setServerUrl(rawUrl) {
  const cleaned = cleanServerUrl(rawUrl);
  _cachedServerUrl = cleaned;

  if (typeof localStorage !== 'undefined') {
    if (cleaned) {
      localStorage.setItem(SERVER_STORAGE_KEY, cleaned);
    } else {
      localStorage.removeItem(SERVER_STORAGE_KEY);
    }
  }

  try {
    if (cleaned) {
      await Preferences.set({ key: SERVER_STORAGE_KEY, value: cleaned });
    } else {
      await Preferences.remove({ key: SERVER_STORAGE_KEY });
    }
  } catch (_e) {
    /* non-fatal fallback to localStorage */
  }

  return cleaned;
}

/**
 * Tests connection to a given server URL by checking the GET /health endpoint.
 */
export async function testServerConnection(targetUrl) {
  const base = cleanServerUrl(targetUrl);
  if (!base) {
    return { ok: false, message: 'Please enter a server URL' };
  }

  try {
    const rawFetch = window.__gdx_raw_fetch__ || window.fetch;
    const res = await rawFetch(`${base}/health`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
    });

    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      return {
        ok: true,
        message: 'Connected successfully',
        status: data.status || 'ok',
        db: data.db || 'ok',
      };
    }
    return { ok: false, message: `Server responded with HTTP ${res.status}` };
  } catch (err) {
    return { ok: false, message: err?.message || 'Network unreachable' };
  }
}

/**
 * Resolves a relative path to target the configured server URL.
 * Preserves external URLs, data URLs, and blob URLs.
 */
export function resolveApiUrl(path) {
  if (!path || typeof path !== 'string') return path;
  if (/^(https?:|\/\/|data:|blob:)/i.test(path)) {
    return path;
  }
  const base = getServerUrl();
  if (!base) {
    return path;
  }
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${base}${normalizedPath}`;
}

/**
 * Installs an ambient fetch interceptor to prefix relative API / auth / portal
 * endpoints with the configured server URL.
 */
export function installFetchInterceptor() {
  if (typeof window === 'undefined' || !window.fetch) return;
  if (window.__gdx_fetch_intercepted__) return;

  const originalFetch = window.fetch;
  window.__gdx_raw_fetch__ = originalFetch;

  window.fetch = async function (input, init) {
    let resolvedInput = input;

    if (typeof input === 'string') {
      if (
        input.startsWith('/api/') ||
        input.startsWith('/auth/') ||
        input.startsWith('/portal/') ||
        input === '/health' ||
        input.startsWith('/health?')
      ) {
        resolvedInput = resolveApiUrl(input);
      }
    } else if (input instanceof URL) {
      const p = input.pathname;
      if (
        p.startsWith('/api/') ||
        p.startsWith('/auth/') ||
        p.startsWith('/portal/') ||
        p === '/health'
      ) {
        resolvedInput = new URL(resolveApiUrl(p + input.search + input.hash));
      }
    } else if (input && typeof input.url === 'string') {
      const u = input.url;
      if (
        u.startsWith('/api/') ||
        u.startsWith('/auth/') ||
        u.startsWith('/portal/') ||
        u === '/health'
      ) {
        resolvedInput = new Request(resolveApiUrl(u), input);
      }
    }

    return originalFetch.call(this, resolvedInput, init);
  };

  window.__gdx_fetch_intercepted__ = true;
}

/**
 * Initializes server configuration on application startup.
 */
export async function initServerConfig() {
  if (_initialized) return _cachedServerUrl;
  // The web app is served by the server it talks to. A saved address there
  // would send every /api and /auth request — the login password included —
  // somewhere else, so only the Android app reroutes.
  if (!Capacitor.isNativePlatform()) return '';

  // Install before the first await: main.js does not await this, and the
  // router's first navigation (the native session restore) fetches straight
  // away. The interceptor resolves the URL per request, so it picks up the
  // localStorage copy now and the Preferences value once it lands.
  installFetchInterceptor();

  try {
    const { value } = await Preferences.get({ key: SERVER_STORAGE_KEY });
    if (value) {
      _cachedServerUrl = cleanServerUrl(value);
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(SERVER_STORAGE_KEY, _cachedServerUrl);
      }
    } else if (typeof localStorage !== 'undefined') {
      _cachedServerUrl = cleanServerUrl(localStorage.getItem(SERVER_STORAGE_KEY) || '');
    }
  } catch (_e) {
    if (typeof localStorage !== 'undefined') {
      _cachedServerUrl = cleanServerUrl(localStorage.getItem(SERVER_STORAGE_KEY) || '');
    }
  }

  _initialized = true;
  return _cachedServerUrl;
}
