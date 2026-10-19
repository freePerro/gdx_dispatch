/**
 * useApi — composable for view-layer API calls.
 *
 * Two layers:
 *   - createApiClient()  : bare transport. No toast, no router. Used by tests
 *                          and any non-Vue-setup context. Throws on error.
 *   - useApi()           : composable. Wraps the transport with status-aware
 *                          error toasts (401 logout, 403 perm, 429 throttle,
 *                          5xx generic, network) AND honors a `successMessage`
 *                          option on every mutation method. Re-throws so
 *                          callers can still try/catch when they need to
 *                          suppress side-effects on failure.
 *
 * The 175 callsites passing `{ successMessage: '...' }` were silently dropped
 * in the prior implementation — that's the root cause of "did it save?"
 * complaints. This file restores the contract callers were already writing
 * against.
 *
 * PrimeVue v4 severity ground truth used here:
 *   - Toast: 'success' | 'info' | 'warn' | 'error' | 'secondary' | 'contrast'
 *     (NOT 'danger' — that's Tag/Button vocabulary).
 */
import { useAuthStore } from '../stores/auth';
import { useToast } from 'primevue/usetoast';
import { useRouter } from 'vue-router';
import { queueAction } from './useOfflineSync';
import { getLoginRedirectLocation } from '../lib/auth-urls';

// The server's refusal for a session whose account must change its password
// first (GDXA-1003). The server sends `{code, message}`; a bare `detail`
// string is accepted too, so a later reshape cannot silently strand the
// person on a page whose every call fails. The store calls below are optional
// because many specs vi.mock the auth store without these members.
export const PASSWORD_CHANGE_REQUIRED = 'password_change_required';
// The self-service change that clears the flag server-side; a 2xx here clears
// it in the store too, so the router guard lets the person go.
const CHANGE_PASSWORD_URL = '/api/users/me/change-password';
const ME_URL = '/api/users/me';

// An outage (a gateway 5xx mid-deploy, a dropped connection) fails every call
// and poll at once, and each used to stack its own toast (GDXA-1490). These
// say the same thing whichever call failed, so one on screen at a time is
// enough. The window is the toast's life: a failure after it has gone (a Save
// a few seconds after a poll failed) still says so. The map is module-level
// because every view and poller holds its own useApi().
export const OUTAGE_TOAST_WINDOW_MS = 4000;
const lastOutageToast = new Map();

function addOutageToast(toast, message) {
  const now = Date.now();
  if (now - (lastOutageToast.get(message.summary) ?? -Infinity) < OUTAGE_TOAST_WINDOW_MS) return;
  lastOutageToast.set(message.summary, now);
  toast.add(message);
}

// For specs: the window outlives a test, so each starts with a clean slate.
export function resetOutageToasts() {
  lastOutageToast.clear();
}

export function isPasswordChangeRequired(err) {
  return err?.status === 403 &&
    (err.code === PASSWORD_CHANGE_REQUIRED || err.message === PASSWORD_CHANGE_REQUIRED);
}

function isHeldOnServer(url, method) {
  if (!String(url).startsWith('/api/')) return false;
  if (method === 'GET' && url === ME_URL) return false;
  if (method === 'POST' && url === CHANGE_PASSWORD_URL) return false;
  return true;
}

export function createApiClient() {
  const auth = useAuthStore();

  async function request(url, options = {}, retry = true) {
    // A held session can do nothing on the server but read /me and change
    // its password (routers/auth/core.py _enforce_password_change), so a
    // shell poll or a store load sent while held only comes back 403 — ten
    // of them on /profile in the 2026-10-19 install walk, each a console
    // error. Refuse those here with the server's own error, without the
    // event: the person is already on the form. /auth/ (login, refresh,
    // logout) checks its own token and is not held.
    if (auth.mustChangePassword && isHeldOnServer(url, options.method || 'GET')) {
      const err = new Error(PASSWORD_CHANGE_REQUIRED);
      err.status = 403;
      err.url = url;
      err.code = PASSWORD_CHANGE_REQUIRED;
      throw err;
    }

    const headers = {
      ...(options.headers || {}),
    };

    if (auth.accessToken) {
      headers.Authorization = `Bearer ${auth.accessToken}`;
    }

    const requestOptions = {
      ...options,
      headers,
      credentials: 'include',
    };

    const response = await fetch(url, requestOptions);

    if (response.status === 401 && retry) {
      try {
        await auth.refreshAccessToken();
      } catch (e) {
        // A refresh that failed without logging out (a network blip, a
        // gateway 5xx mid-deploy) keeps the session: rethrow as-is. A refused refresh has logged out, so
        // this request is a 401 like any other — the status sends the caller
        // down the session-expired path rather than a bare "Token refresh failed".
        if (auth.isAuthenticated) throw e;
        const err = new Error('Session expired');
        err.status = 401;
        err.url = url;
        throw err;
      }
      return request(url, options, false);
    }

    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      let errBody = null;
      try {
        errBody = await response.json();
        const rawDetail = errBody.detail ?? errBody.error ?? errBody.message;
        if (typeof rawDetail === 'string') detail = rawDetail;
        // A structured detail — `{code, message}` — is how an endpoint says
        // "this refusal has a machine-readable kind". Render its `message`
        // rather than JSON.stringify'ing the whole object at the user, which
        // is what used to happen and produced `{"code":"…","message":"…"}` in
        // a toast. The code rides along on err.code for callers that branch.
        else if (rawDetail && typeof rawDetail === 'object' && typeof rawDetail.message === 'string') {
          detail = rawDetail.message;
        }
        // require_role's `{error_type, detail, required_roles}` carries its
        // reason in `detail` (GDXA-1144).
        else if (rawDetail && typeof rawDetail === 'object' && typeof rawDetail.detail === 'string') {
          detail = rawDetail.detail;
        }
        else if (rawDetail !== undefined && rawDetail !== null) detail = JSON.stringify(rawDetail);
      } catch {}
      const err = new Error(detail);
      err.status = response.status;
      err.url = url;
      err.code = (errBody && typeof errBody.detail === 'object' && errBody.detail)
        ? errBody.detail.code
        : undefined;
      // Attach the parsed JSON body so callers can read structured error
      // fields (e.g. `missing[]` from /api/jobs/{id}/complete on 422).
      // The pre-2026-05-10 behavior dropped everything except `detail`,
      // which silently broke the dispatch "Cannot complete: parts missing"
      // toast — the missing array was being read off a non-existent field.
      err.body = errBody;
      if (isPasswordChangeRequired(err)) {
        // The server refuses this session until its password changes. Flag
        // it and let the router (which listens for the event) send the person
        // to the change-password form rather than leave them on a page
        // whose every call now fails (GDXA-1004).
        err.code = PASSWORD_CHANGE_REQUIRED;
        auth.setMustChangePassword?.(true);
        window.dispatchEvent(new CustomEvent('gdx:password-change-required'));
        // An expected refusal, not a defect: no console error and no
        // client-error report. Every shell poll gets it while the person is
        // on /profile, and each would otherwise land in the errors table.
        throw err;
      }
      console.error(`API ${options.method || 'GET'} ${url} → ${response.status}: ${detail}`);

      // Report error to backend for CC R&D tracking
      try {
        fetch('/api/feedback/client-error', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: url,
            method: options.method || 'GET',
            status: response.status,
            detail: detail,
            page: window.location.pathname,
            timestamp: new Date().toISOString(),
          }),
        }).catch(() => {});
      } catch {}

      throw err;
    }

    if ((options.method || 'GET') === 'POST' && url === CHANGE_PASSWORD_URL) {
      auth.setMustChangePassword?.(false);
    }

    if (response.status === 204) {
      return null;
    }
    // Decide "empty" from the body, not the Content-Length header: the
    // Capacitor native HTTP bridge reports `content-length: 0` on chunked
    // responses that do carry a body, which made every call on the Android
    // app resolve to null.
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    // /me carries the live flag, and /profile reads it on every load (and
    // Refresh): a password reset by link or changed in another tab is picked
    // up there too, not only when the router re-asks on the way out.
    if ((options.method || 'GET') === 'GET' && url === ME_URL &&
        typeof data?.must_change_password === 'boolean') {
      auth.setMustChangePassword?.(data.must_change_password);
    }
    return data;
  }

  function get(url) {
    return request(url);
  }

  function post(url, data) {
    const isForm = typeof FormData !== 'undefined' && data instanceof FormData;
    if (isForm) {
      return request(url, { method: 'POST', body: data });
    }
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  }

  /**
   * Phase 3 — POST that queues for offline replay if the network is
   * unreachable. Use this from mobile mutation paths where the tech can
   * be in a dead zone (en-route, arrived, complete, parts-needed,
   * notes, status updates). Synchronous-looking from the caller's POV:
   * resolves immediately with the server response when online, OR a
   * `{ queued: true, idempotency_key }` stub when offline. Idempotency
   * key flows server-side as X-Idempotency-Key on replay.
   */
  function postQueued(url, data, opts = {}) {
    return queueAction('POST', url, data, opts);
  }
  function patchQueued(url, data, opts = {}) {
    return queueAction('PATCH', url, data, opts);
  }

  function patch(url, data) {
    return request(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  }

  function put(url, data) {
    return request(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  }

  function del(url, data) {
    // Optional JSON body — DELETE /api/push/v2/unsubscribe reads a Pydantic
    // body ({ endpoint }) and was the only caller sending one.
    if (data === undefined) {
      return request(url, { method: 'DELETE' });
    }
    return request(url, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  }

  // `delete` is a legal property name (only the operator is reserved).
  // Nine callsites were written against `api.delete(...)` and silently
  // invoked `undefined` — every delete button they backed was dead.
  return { request, get, post, put, patch, del, delete: del, postQueued, patchQueued };
}

/**
 * View-facing composable. Use this in `<script setup>`.
 *
 * Mutation methods accept an optional `options` arg:
 *   { successMessage?: string, suppressErrorToast?: boolean | (err) => boolean }
 *
 * Examples:
 *   const api = useApi();
 *   await api.post('/api/foo', payload, { successMessage: 'Foo created' });
 *   await api.del(`/api/foo/${id}`, { successMessage: 'Foo deleted' });
 *   const data = await api.get('/api/list');   // get is unchanged signature
 */
export function useApi() {
  const transport = createApiClient();

  // useToast / useRouter / useAuthStore must be called inside Vue setup.
  // Wrap in try/catch so direct `useApi()` from non-setup code (rare) still works.
  let toast = null;
  let router = null;
  let auth = null;
  try { toast = useToast(); } catch {}
  try { router = useRouter(); } catch {}
  try { auth = useAuthStore(); } catch {}

  function fireSuccess(msg) {
    if (msg && toast) {
      toast.add({ severity: 'success', summary: msg, life: 3000 });
    }
  }

  function fireError(err, suppress) {
    // suppress: true silences every toast; a predicate silences only the
    // errors it accepts (an expected 403), so the rest keep the handling
    // below, the 401 logout included (GDXA-1143).
    if (!toast || (typeof suppress === 'function' ? suppress(err) : suppress)) return;
    const status = err?.status || 0;
    const msg = err?.message || '';

    if (status === 401 || /Unauthorized/i.test(msg)) {
      // No toast: one per failing call stacked up on the board (2026-10-15),
      // and the first call to meet a dead session is often a background poll
      // that never comes here. The login page says why the person is there
      // (`reason=expired`), once, whichever call noticed.
      if (auth?.isAuthenticated) auth.logout();
      const current = router?.currentRoute?.value;
      if (router && current?.path !== '/login' && !current?.meta?.public) {
        router.push(current?.fullPath
          ? getLoginRedirectLocation(current.fullPath, 'expired')
          : { path: '/login', query: { reason: 'expired' } }).catch(() => {});
      }
    } else if (isPasswordChangeRequired(err)) {
      // No toast: the AppLayout banner says it once, and shell polls hitting
      // this 403 on /profile stacked a toast per call (2026-10-14 walk).
    } else if (status === 403 || /Forbidden|Missing permission/i.test(msg)) {
      // Only core RBAC's refusal is fixed by a grant on /role-permissions:
      // `Missing permission: ['billing.edit']` (require_permission),
      // `Missing permission: plugin.<key>.<action>` (the plugin proxy). A bare
      // `Forbidden` (FastAPI's detail for HTTPException(403); no core route
      // sends one as of 2026-10-14) keeps the advice. Under /api/plugins it is
      // the plugin's own `HTTPException(403)`, which the proxy passes through
      // unchanged. Any other 403 (a plugin's own refusal, a disabled module, a
      // role-gated route) says its own reason, without advice that cannot help
      // (GDXA-1144).
      const text = msg.trim();
      const match = text.match(/^Missing permission:\s*(\[[^\]]*\]|plugin\.[a-z0-9][a-z0-9_-]*\.(?:read|write))$/);
      const fromPlugin = /^\/api\/plugins(?:[/?]|$)/.test(err?.url || '');
      let detail;
      if (match) {
        detail = `You don't have ${match[1]}. Ask an admin to grant it on /role-permissions.`;
      } else if ((text === '' || text === 'Forbidden') && !fromPlugin) {
        detail = "You don't have permission to do that. Ask an admin to grant it on /role-permissions.";
      } else if (text === '' || text === 'Forbidden' || /^[[{]/.test(text) || /^Request failed \(403\)$/.test(text)) {
        // No reason of its own, a JSON-stringified structured detail, or a
        // non-JSON body (core always answers JSON, so that one is not core).
        detail = "You don't have permission to do that.";
      } else {
        detail = msg;
      }
      toast.add({ severity: 'warn', summary: 'Permission denied', detail, life: 5000 });
    } else if (status === 429) {
      toast.add({ severity: 'warn', summary: 'Slow down', detail: 'Too many requests — try again in a moment.', life: 4000 });
    } else if (status >= 500) {
      addOutageToast(toast, { severity: 'error', summary: 'Something went wrong', detail: 'Please try again.', life: OUTAGE_TOAST_WINDOW_MS });
    } else if (/Failed to fetch|NetworkError/i.test(msg)) {
      addOutageToast(toast, { severity: 'error', summary: 'Network error', detail: 'Check your connection', life: OUTAGE_TOAST_WINDOW_MS });
    } else {
      // Generic fallback for any unclassified error. A refused token refresh
      // arrives as the 401 above, not here.
      toast.add({ severity: 'error', summary: 'Error', detail: msg || 'Unknown error', life: 4000 });
    }
  }

  async function get(url, options = {}) {
    try { return await transport.get(url); }
    catch (e) { fireError(e, options.suppressErrorToast); throw e; }
  }

  async function post(url, data, options = {}) {
    try {
      const result = await transport.post(url, data);
      fireSuccess(options.successMessage);
      return result;
    } catch (e) { fireError(e, options.suppressErrorToast); throw e; }
  }

  async function patch(url, data, options = {}) {
    try {
      const result = await transport.patch(url, data);
      fireSuccess(options.successMessage);
      return result;
    } catch (e) { fireError(e, options.suppressErrorToast); throw e; }
  }

  async function put(url, data, options = {}) {
    try {
      const result = await transport.put(url, data);
      fireSuccess(options.successMessage);
      return result;
    } catch (e) { fireError(e, options.suppressErrorToast); throw e; }
  }

  async function del(url, options = {}) {
    try {
      const result = await transport.del(url);
      fireSuccess(options.successMessage);
      return result;
    } catch (e) { fireError(e, options.suppressErrorToast); throw e; }
  }

  // Alias with a distinct signature: delete(url, data?, options?).
  // The existing `api.delete(...)` callsites were written against this shape
  // (PayrollView passes (url, {}, { successMessage }); push unsubscribe passes
  // (url, { endpoint })) — `del(url, options)` keeps its old contract.
  async function _delete(url, data, options = {}) {
    // Guard the natural mistake of mirroring del's signature —
    // delete(url, { successMessage }) — which would otherwise silently send
    // the options object as the request body and drop the toast.
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      const keys = Object.keys(data);
      if (keys.length > 0 && keys.every((k) => k === 'successMessage' || k === 'suppressErrorToast')) {
        options = data;
        data = undefined;
      }
    }
    try {
      const result = await transport.del(url, data);
      fireSuccess(options.successMessage);
      return result;
    } catch (e) { fireError(e, options.suppressErrorToast); throw e; }
  }

  async function request(url, options = {}, fetchOptions = {}) {
    try {
      const result = await transport.request(url, options);
      if (fetchOptions.successMessage) fireSuccess(fetchOptions.successMessage);
      return result;
    } catch (e) { fireError(e, fetchOptions.suppressErrorToast); throw e; }
  }

  return {
    request, get, post, put, patch, del,
    delete: _delete,
    postQueued: transport.postQueued,
    patchQueued: transport.patchQueued,
  };
}
