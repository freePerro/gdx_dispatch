/**
 * Auth store — hasPermission gate, incl. the admin lockout guard.
 *
 * Regression guard for the /audit finding: an admin/owner whose resolved
 * permission set comes back EMPTY (a backend resolver fallback serialized as
 * HTTP 200 `[]`) must NOT be locked out of their own nav — the escape hatch
 * stays for an empty loaded set. A non-empty loaded set is authoritative.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../auth';

// Minimal unsigned JWT with the given role claim (store only reads the payload).
function tokenFor(role) {
  const payload = btoa(JSON.stringify({ role }))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `h.${payload}.s`;
}

function storeWith({ role, loaded, perms }) {
  if (role) sessionStorage.setItem('gdx_access_token', tokenFor(role));
  const store = useAuthStore();
  if (perms) store.permissions = new Set(perms);
  if (loaded !== undefined) store.permissionsLoaded = loaded;
  return store;
}

describe('auth store — hasPermission', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    sessionStorage.clear();
  });

  it('returns false when not authenticated', () => {
    const store = useAuthStore();
    expect(store.hasPermission('settings.write')).toBe(false);
  });

  it('returns true for an empty key (no gate)', () => {
    const store = storeWith({ role: 'tech', loaded: true, perms: [] });
    expect(store.hasPermission('')).toBe(true);
  });

  it('admin pre-load: escape hatch grants everything', () => {
    const store = storeWith({ role: 'admin', loaded: false, perms: [] });
    expect(store.hasPermission('settings.write')).toBe(true);
  });

  it('admin with a NON-EMPTY loaded set is authoritative (no blanket grant)', () => {
    const store = storeWith({ role: 'admin', loaded: true, perms: ['jobs.read_all'] });
    expect(store.hasPermission('jobs.read_all')).toBe(true);
    expect(store.hasPermission('settings.write')).toBe(false); // lacks it → hidden
  });

  it('admin with an EMPTY loaded set keeps the escape hatch (NO lockout)', () => {
    // The bug guard: resolver fallback returns [] as HTTP 200 — must not collapse
    // the admin's nav.
    const store = storeWith({ role: 'admin', loaded: true, perms: [] });
    expect(store.hasPermission('settings.write')).toBe(true);
    expect(store.hasPermission('anything')).toBe(true);
  });

  it('owner empty loaded set also keeps the hatch', () => {
    const store = storeWith({ role: 'owner', loaded: true, perms: [] });
    expect(store.hasPermission('settings.write')).toBe(true);
  });

  it('wildcard grants everything', () => {
    const store = storeWith({ role: 'tech', loaded: true, perms: ['*'] });
    expect(store.hasPermission('settings.write')).toBe(true);
  });

  it('non-admin: loaded set is authoritative (has key → true, lacks → false)', () => {
    const store = storeWith({ role: 'tech', loaded: true, perms: ['jobs.read_own'] });
    expect(store.hasPermission('jobs.read_own')).toBe(true);
    expect(store.hasPermission('settings.write')).toBe(false);
  });

  it('non-admin with an EMPTY loaded set does NOT get the escape hatch', () => {
    // Only admin/owner get the empty-set hatch; a stripped tech stays stripped.
    const store = storeWith({ role: 'tech', loaded: true, perms: [] });
    expect(store.hasPermission('jobs.read_own')).toBe(false);
  });
});
