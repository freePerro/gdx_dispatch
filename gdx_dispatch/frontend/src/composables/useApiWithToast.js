/**
 * @deprecated 2026-05-09 — useApi now provides toast + status-aware error
 * handling natively. This file re-exports useApi as a backward-compat alias
 * for the ~12 components/views that imported `useApiWithToast`. New code
 * should use `useApi` directly.
 *
 * Behavior is now identical to useApi(). The split was the cause of
 * "did it save?" silent-success bugs — 175 callsites passed
 * `{ successMessage: '...' }` to the base useApi() which silently dropped it.
 */
export { useApi as useApiWithToast } from './useApi';
