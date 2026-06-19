/*
 * SS-13 Slice F — Centralized auth URL builders.
 *
 * One source of truth for the shape of /login?redirect=<returnTo>, shared
 * by LoginPicker.vue (produces the URL) and LoginView.vue (consumes the
 * query param). Avoids drift between the producer and consumer so a
 * future change to the query key or encoding lands in one place.
 */

const DEFAULT_POST_LOGIN_PATH = '/dashboard'

/**
 * Canonical return-to path for the login picker view. Shared by
 * LoginPicker.vue (producer of the `?redirect=` query param) so the
 * constant stays colocated with `getLoginRedirectUrl` below and any
 * future caller that needs to link back to the picker.
 */
export const LOGIN_PICKER_RETURN_TO = '/login-picker'

/**
 * Build the /login URL with a redirect-back query parameter pointing at
 * the caller's current view. The path is URI-encoded so any query string
 * or special characters survive the round-trip through LoginView.
 *
 * @param {string} returnTo - Absolute path (beginning with '/') to return
 *   to after a successful login. Falsy values degrade to bare '/login'.
 * @returns {string}
 */
export function getLoginRedirectUrl(returnTo) {
  if (!returnTo) {
    return '/login'
  }
  return `/login?redirect=${encodeURIComponent(returnTo)}`
}

/**
 * Build a vue-router location object pointing at /login with a redirect-back
 * query param. Same semantic as `getLoginRedirectUrl` but returns the
 * object form used by vue-router navigation guards (`beforeEach` return
 * value, `router.push`, `router.replace`). Keeping the producer here
 * colocates the string-form and object-form builders so a future change
 * to the query key or path lands in one place.
 *
 * @param {string} returnTo - Absolute path (beginning with '/') to return
 *   to after a successful login. Passed through as-is; vue-router handles
 *   URL encoding of the `query` object on serialization.
 * @returns {{path: string, query: {redirect: string}}}
 */
export const getLoginRedirectLocation = (returnTo) => ({ path: '/login', query: { redirect: returnTo } });

/**
 * Resolve the post-login destination from a vue-router route, falling
 * back to the default dashboard path when the redirect query param is
 * missing or empty.
 *
 * @param {{query?: Record<string, any>}} route - vue-router route (or any
 *   object with a `query` shape).
 * @param {string} [fallback] - Path to use when no redirect is present.
 * @returns {string}
 */
export function getPostLoginRedirect(route, fallback = DEFAULT_POST_LOGIN_PATH) {
  const redirect = route && route.query ? route.query.redirect : ''
  return redirect || fallback
}
