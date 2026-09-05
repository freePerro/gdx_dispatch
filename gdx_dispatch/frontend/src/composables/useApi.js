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

export function createApiClient() {
  const auth = useAuthStore();

  async function request(url, options = {}, retry = true) {
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
      await auth.refreshAccessToken();
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
        else if (rawDetail !== undefined && rawDetail !== null) detail = JSON.stringify(rawDetail);
      } catch {}
      const err = new Error(detail);
      err.status = response.status;
      err.code = (errBody && typeof errBody.detail === 'object' && errBody.detail)
        ? errBody.detail.code
        : undefined;
      // Attach the parsed JSON body so callers can read structured error
      // fields (e.g. `missing[]` from /api/jobs/{id}/complete on 422).
      // The pre-2026-05-10 behavior dropped everything except `detail`,
      // which silently broke the dispatch "Cannot complete: parts missing"
      // toast — the missing array was being read off a non-existent field.
      err.body = errBody;
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

    if (response.status === 204 || response.headers?.get?.('content-length') === '0') {
      return null;
    }
    return response.json();
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
 *   { successMessage?: string, suppressErrorToast?: boolean }
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
    if (suppress || !toast) return;
    const status = err?.status || 0;
    const msg = err?.message || '';

    if (status === 401 || /Unauthorized/i.test(msg)) {
      toast.add({ severity: 'warn', summary: 'Session expired', detail: 'Please log in again', life: 3000 });
      if (auth) auth.logout();
      if (router) router.push('/login');
    } else if (status === 403 || /Forbidden|Missing permission/i.test(msg)) {
      const match = msg.match(/Missing permission:\s*(\[[^\]]+\])/i);
      const detail = match
        ? `You don't have ${match[1]}. Ask an admin to grant it on /role-permissions.`
        : "You don't have permission to do that. Ask an admin to grant it on /role-permissions.";
      toast.add({ severity: 'warn', summary: 'Permission denied', detail, life: 5000 });
    } else if (status === 429) {
      toast.add({ severity: 'warn', summary: 'Slow down', detail: 'Too many requests — try again in a moment.', life: 4000 });
    } else if (status >= 500) {
      toast.add({ severity: 'error', summary: 'Something went wrong', detail: 'Please try again.', life: 4000 });
    } else if (/Failed to fetch|NetworkError/i.test(msg)) {
      toast.add({ severity: 'error', summary: 'Network error', detail: 'Check your connection', life: 4000 });
    } else {
      // Generic fallback — covers throws from refreshAccessToken failures
      // (status=0, msg="Token refresh failed") and any other unclassified error.
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
