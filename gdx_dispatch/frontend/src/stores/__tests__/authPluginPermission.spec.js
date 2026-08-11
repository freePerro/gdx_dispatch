/**
 * hasPluginPermission — the frontend half of ADR-013 per-plugin authorization.
 *
 * The trap this exists for: `hasPermission` is an exact Set lookup, and the
 * builtin admin contract holds the BLANKET `plugins.read` and can never hold a
 * per-plugin key (those aren't in the static catalog). Gating plugin nav on the
 * specific key alone would hide every plugin from admins while the API served
 * them happily — a silent, role-specific disappearance nobody testing as the
 * owner would ever see.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../auth';

// Same shape as auth.spec.js: authentication comes from a token in
// sessionStorage and the role is read from its payload.
function tokenFor(role) {
  const payload = btoa(JSON.stringify({ role }))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `h.${payload}.s`;
}

function authWith(perms, { role = 'dispatcher', loaded = true } = {}) {
  sessionStorage.setItem('gdx_access_token', tokenFor(role));
  const auth = useAuthStore();
  auth.permissions = new Set(perms);
  auth.permissionsLoaded = loaded;
  return auth;
}

describe('hasPluginPermission', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    sessionStorage.clear();
  });

  it('accepts the specific per-plugin grant', () => {
    const auth = authWith(['plugin.chipricing.read']);
    expect(auth.hasPluginPermission('chipricing', 'read')).toBe(true);
  });

  it('accepts the blanket grant — what an admin actually holds', () => {
    const auth = authWith(['plugins.read']);
    expect(auth.hasPluginPermission('chipricing', 'read')).toBe(true);
    expect(auth.hasPluginPermission('midland', 'read')).toBe(true);
  });

  it('scopes a per-plugin grant to that plugin', () => {
    const auth = authWith(['plugin.chipricing.read']);
    expect(auth.hasPluginPermission('midland', 'read')).toBe(false);
  });

  it('does not let read imply write', () => {
    const auth = authWith(['plugin.chipricing.read', 'plugins.read']);
    expect(auth.hasPluginPermission('chipricing', 'write')).toBe(false);
  });

  it('refuses a user with no plugin grant at all', () => {
    const auth = authWith(['jobs.read_own', 'mobile.use'], { role: 'technician' });
    expect(auth.hasPluginPermission('chipricing', 'read')).toBe(false);
  });

  it('passes the owner wildcard', () => {
    const auth = authWith(['*'], { role: 'owner' });
    expect(auth.hasPluginPermission('anything', 'write')).toBe(true);
  });

  it('defaults to the read action', () => {
    const auth = authWith(['plugins.read']);
    expect(auth.hasPluginPermission('chipricing')).toBe(true);
  });

  it('refuses an empty plugin key even for the wildcard', () => {
    const auth = authWith(['*'], { role: 'owner' });
    expect(auth.hasPluginPermission('')).toBe(false);
    expect(auth.hasPluginPermission(undefined)).toBe(false);
  });

  it('keeps the admin escape hatch while permissions are still loading', () => {
    // Mirrors hasPermission: an admin must never be locked out of their own
    // tenant by a not-yet-resolved permission set.
    const auth = authWith([], { role: 'admin', loaded: false });
    expect(auth.hasPluginPermission('chipricing', 'read')).toBe(true);
  });
});
