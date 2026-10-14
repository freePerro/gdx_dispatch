/**
 * useOfficeAlertsKeeper — keeps "Alerts on this phone" matched to whoever is
 * signed in. Called once by AppLayout, which every signed-in page renders at
 * any width (the bottom nav is narrow-screen only, and a phone in landscape
 * or a tablet is wider).
 *
 * Owner Domain: mobile-tech (see .claude/agents/mobile-tech.md)
 *
 * The user id comes from the access token: after a cold start the session is
 * restored by a refresh that sets the token but not `auth.user`.
 */
import { watch } from 'vue';
import { useAuthStore } from '../stores/auth';
import { usePermission } from './usePermission';
import { createApiClient } from './useApi';
import { isOfficeAlertsAvailable, syncOfficeAlerts } from '../lib/officeAlerts';

export function useOfficeAlertsKeeper() {
  if (!isOfficeAlertsAvailable()) return;
  const auth = useAuthStore();
  const { hasPermission } = usePermission();
  // The owner is checked when the signed-in user changes, and an office
  // user's service is kept (token re-minted when due) once per user. The
  // getter re-runs on every token refresh and permission reload; those reach
  // here and stop.
  let checkedFor = '';
  let keptFor = '';
  watch(
    () => [String(auth.claims?.sub || auth.user?.id || ''), hasPermission('nav.office')],
    ([userId, office]) => {
      if (!userId) return;
      const keep = office && keptFor !== userId;
      if (!keep && checkedFor === userId) return;
      checkedFor = userId;
      if (keep) keptFor = userId;
      syncOfficeAlerts(createApiClient(), { userId, office: keep });
    },
    { immediate: true },
  );
}
