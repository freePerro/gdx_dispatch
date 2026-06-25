import { computed, ref } from 'vue';
import { defineStore } from 'pinia';

/* Auth store — tenant resolved server-side via subdomain aliases */
export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref(sessionStorage.getItem('gdx_access_token') || null);
  // Hydrate user from sessionStorage on store init so the topbar +
  // user menu have a name/role to render after a hard reload. Login
  // writes here (see below); logout clears.
  const _persistedUser = (() => {
    try {
      const raw = sessionStorage.getItem('gdx_user');
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  })();
  const user = ref(_persistedUser);
  const tenantSlug = ref(sessionStorage.getItem('gdx_tenant_slug') || null);
  const permissions = ref(new Set());
  const permissionsLoaded = ref(false);

  const isAuthenticated = computed(() => Boolean(accessToken.value));

  function _decodeJwt(token) {
    if (!token) return null;
    const parts = token.split('.');
    if (parts.length < 2) return null;
    try {
      // base64url → base64
      const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
      const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
      return JSON.parse(atob(padded));
    } catch {
      return null;
    }
  }

  const claims = computed(() => _decodeJwt(accessToken.value));
  const role = computed(() => (claims.value?.role || '').toLowerCase());
  const isAdmin = computed(() => role.value === 'admin' || role.value === 'owner');

  function _tenantHeader() {
    // Priority: 1) sessionStorage slug, 2) subdomain, 3) hardcoded fallback
    const stored = tenantSlug.value || sessionStorage.getItem('gdx_tenant_slug');
    if (stored) {
      return { 'x-tenant-id': stored };
    }
    const parts = window.location.hostname.split('.');
    const sub = parts.length >= 3 ? parts[0] : null;
    if (sub && sub !== 'www') {
      // Send raw subdomain — backend resolves aliases via _SUBDOMAIN_ALIASES
      return { 'x-tenant-id': sub };
    }
    // No tenant detected — login will fail with "Unknown tenant" which
    // prompts the user to enter their company name
    return {};
  }

  async function login(credentials) {
    // Store company slug for tenant resolution
    if (credentials.company) {
      sessionStorage.setItem('gdx_tenant_slug', credentials.company);
      tenantSlug.value = credentials.company;
    }

    let response;
    try {
      response = await fetch('/auth/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ..._tenantHeader(),
        },
        credentials: 'include',
        body: JSON.stringify({ email: credentials.email, password: credentials.password }),
      });
    } catch (err) {
      // Network failure mid-attempt: invalidate any prior session so a
      // disconnected kiosk doesn't keep operating as the prior user.
      _clearSession();
      await _serverLogout();
      throw err;
    }

    if (!response.ok) {
      // Failed login MUST invalidate any prior session — the person at
      // the keyboard is trying to authenticate as someone else. Leaving
      // the prior token in sessionStorage means the route guard's
      // `isAuthenticated` stays true and the user can keep operating as
      // the prior identity. See _clearSession() for the 2026-05-09
      // incident this closes.
      _clearSession();
      await _serverLogout();
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Login failed');
    }

    const data = await response.json();
    accessToken.value = data.access_token;
    sessionStorage.setItem('gdx_access_token', data.access_token);
    user.value = data.user || null;
    if (user.value) {
      try { sessionStorage.setItem('gdx_user', JSON.stringify(user.value)); } catch { /* ignore */ }
    } else {
      sessionStorage.removeItem('gdx_user');
    }
    loadPermissions({ force: true }).catch(() => {});

    return data;
  }

  // Wipe every trace of an authenticated session — store refs, the
  // sessionStorage shadows the store hydrates from, and the permission
  // cache. Used by `logout()` and (critically) by every failure path in
  // login() so a wrong-password attempt doesn't leave the
  // PRIOR user's session intact in sessionStorage. 2026-05-09 incident:
  // chrome-devtools browser carried a stale admin token; my login as
  // auditor28 returned "Invalid credentials" but the route guard's
  // `isAuthenticated = Boolean(accessToken.value)` was still true and
  // /dashboard rendered as the prior admin. Same bug applies to any
  // shared kiosk / shared workstation: typing the wrong password into
  // someone else's session leaves you operating as them.
  function _clearSession() {
    accessToken.value = null;
    user.value = null;
    tenantSlug.value = null;
    permissions.value = new Set();
    permissionsLoaded.value = false;
    sessionStorage.removeItem('gdx_access_token');
    sessionStorage.removeItem('gdx_tenant_slug');
    sessionStorage.removeItem('gdx_user');
  }

  // Server-side logout: revokes the refresh cookie so a re-auth can't
  // skate by on the prior session's still-valid HttpOnly cookie. Best
  // effort — a 401/network-error here doesn't block the local clear.
  async function _serverLogout() {
    try {
      await fetch('/auth/logout', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        // logout() does NOT await this and the refresh-401 path throws +
        // navigates immediately after. Without keepalive the browser
        // cancels the in-flight request on page/route unload and the
        // server-side Set-Cookie Max-Age=0 never lands — which is the
        // exact dead-cookie loop this whole fix exists to kill. keepalive
        // lets the POST outlive the document. (Bodyless, so well under
        // the 64 KB keepalive cap.)
        keepalive: true,
      });
    } catch (_) {
      /* swallow — local clear is the primary guarantee */
    }
  }

  function logout() {
    // Fire the server-side logout FIRST so the still-attached HttpOnly
    // refresh cookie rides along on the request. JS can't delete that
    // cookie (httpOnly + domain=.example.com) — only the server's
    // Set-Cookie Max-Age=0 can. Without this, a refresh-failure logout
    // leaves the dead cookie in place; the next background refresh resends
    // it, the server's replay detector 401s + sinks an error, logout()
    // fires again as a no-op, and the loop refloods CC support/errors
    // (6,717 rows over 4 days, 2026-05-14). Fire-and-forget by design —
    // _serverLogout swallows its own errors and the local clear is the
    // primary guarantee, so we must not await it.
    _serverLogout();
    _clearSession();
    try {
      window.dispatchEvent(new CustomEvent('gdx:auth-logout'));
    } catch { /* swallow */ }
  }

  // Resolved permission set for the current user. Backend is the only
  // enforcer; this is UX-side filtering (sidebar, v-if, route guards).
  let _permissionsInFlight = null;
  async function loadPermissions({ force = false } = {}) {
    if (!isAuthenticated.value) {
      permissions.value = new Set();
      permissionsLoaded.value = false;
      return permissions.value;
    }
    if (permissionsLoaded.value && !force) return permissions.value;
    if (_permissionsInFlight && !force) return _permissionsInFlight;
    _permissionsInFlight = (async () => {
      try {
        const res = await fetch('/api/users/me/permissions', {
          credentials: 'include',
          headers: {
            Authorization: `Bearer ${accessToken.value}`,
            ..._tenantHeader(),
          },
        });
        if (!res.ok) {
          permissions.value = new Set();
          permissionsLoaded.value = false;
          return permissions.value;
        }
        const data = await res.json();
        permissions.value = new Set(Array.isArray(data.permissions) ? data.permissions : []);
        permissionsLoaded.value = true;
        return permissions.value;
      } finally {
        _permissionsInFlight = null;
      }
    })();
    return _permissionsInFlight;
  }

  // Repopulate `user` from /api/users/me when a session is restored from
  // sessionStorage but the persisted user shape is missing fields (or the
  // whole object). Pre-fix the mobile user menu rendered "Signed in" with
  // no name/email when a session pre-dated the login user_payload addition,
  // or when a refresh path dropped the user object. Fire-and-forget: this
  // hydration is purely cosmetic for the menu — every API call still
  // re-derives identity from the JWT server-side.
  let _hydrateUserInFlight = null;
  async function hydrateUser({ force = false } = {}) {
    if (!isAuthenticated.value) return null;
    if (!force && user.value && user.value.email && user.value.name) return user.value;
    if (_hydrateUserInFlight) return _hydrateUserInFlight;
    _hydrateUserInFlight = (async () => {
      try {
        const res = await fetch('/api/users/me', {
          credentials: 'include',
          headers: {
            Authorization: `Bearer ${accessToken.value}`,
            ..._tenantHeader(),
          },
        });
        if (!res.ok) return null;
        const data = await res.json();
        // /api/users/me serializer returns {id, email, full_name|name, role, ...}
        const merged = {
          id: data.id || user.value?.id,
          email: data.email || user.value?.email,
          name: data.name || data.full_name || user.value?.name,
          full_name: data.full_name || data.name || user.value?.full_name,
          role: data.role || user.value?.role,
        };
        user.value = merged;
        try { sessionStorage.setItem('gdx_user', JSON.stringify(merged)); } catch { /* ignore */ }
        return merged;
      } catch (_) {
        return null;
      } finally {
        _hydrateUserInFlight = null;
      }
    })();
    return _hydrateUserInFlight;
  }

  // Trigger best-effort hydration on store init so the topbar menu has a
  // name/email by the time the user opens it.
  if (isAuthenticated.value && (!_persistedUser || !_persistedUser.email)) {
    hydrateUser().catch(() => {});
  }

  function hasPermission(key) {
    if (!isAuthenticated.value) return false;
    if (!key) return true;
    // Admin/owner escape hatch — kept BEFORE the set loads AND when the loaded
    // set is empty. This is UX-only filtering (the backend is the sole enforcer).
    // NOTE: `isAdmin` derives from the JWT `role` claim, which APPROXIMATES but
    // does NOT mirror the backend — the backend resolves admin/owner from
    // User.role in the tenant DB (core/modules.py) so it can ignore a stale JWT
    // role. On a stale JWT the two disagree; acceptable here because the API
    // still 403s anything the real role can't do.
    // A NON-EMPTY loaded set is authoritative, so an admin whose caps lack a key
    // (e.g. settings.write) no longer sees pages the API denies. An *empty*
    // resolved set for an admin means the backend resolver hit a fallback
    // (missing tenant/user_id, role-lookup error) and returned `[]` as HTTP 200 —
    // never a real grant — so it must NOT collapse the admin's nav and lock them
    // out of their own tenant.
    if (isAdmin.value && (!permissionsLoaded.value || permissions.value.size === 0)) return true;
    if (permissions.value.has('*')) return true;
    return permissions.value.has(key);
  }

  // Single-flight refresh: concurrent 401s share one /auth/refresh POST.
  // Without this, parallel API calls each fire their own refresh and the
  // server's replay detector logs refresh_replay_detected and force-logs-out.
  let _refreshInFlight = null;

  async function refreshAccessToken() {
    if (_refreshInFlight) return _refreshInFlight;

    _refreshInFlight = (async () => {
      const response = await fetch('/auth/refresh', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          ..._tenantHeader(),
        },
      });

      if (!response.ok) {
        logout();
        throw new Error('Token refresh failed');
      }

      const data = await response.json();
      accessToken.value = data.access_token;
      sessionStorage.setItem('gdx_access_token', data.access_token);

      return data.access_token;
    })();

    try {
      return await _refreshInFlight;
    } finally {
      _refreshInFlight = null;
    }
  }

  return {
    accessToken,
    user,
    tenantSlug,
    permissions,
    permissionsLoaded,
    isAuthenticated,
    claims,
    role,
    isAdmin,
    login,
    logout,
    refreshAccessToken,
    loadPermissions,
    hasPermission,
    hydrateUser,
  };
});
