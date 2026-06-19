import { computed, onMounted } from 'vue';
import { useAuthStore } from '../stores/auth';

/**
 * Reactive permission check. Mirrors backend require_permission():
 * - admins/owners (legacy role) always pass
 * - WILDCARD '*' grants everything
 * - otherwise the permission must be in the resolved set
 *
 * Backend remains the only source of truth. This composable is UX glue —
 * use it to hide buttons (`v-if="hasPermission('invoices.write')"`),
 * filter sidebar entries, and short-circuit route guards.
 */
export function usePermission() {
  const auth = useAuthStore();

  onMounted(() => {
    if (!auth.permissionsLoaded) {
      auth.loadPermissions().catch(() => {});
    }
  });

  // Reactive computed so v-if updates if permissions reload (e.g. role change).
  const hasPermission = (key) => auth.hasPermission(key);
  const permissions = computed(() => auth.permissions);
  const permissionsLoaded = computed(() => auth.permissionsLoaded);

  return {
    hasPermission,
    permissions,
    permissionsLoaded,
    reloadPermissions: () => auth.loadPermissions({ force: true }),
  };
}
