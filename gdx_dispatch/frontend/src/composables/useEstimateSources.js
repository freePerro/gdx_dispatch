/**
 * useEstimateSources — discovery of estimate_source plugin providers (ADR-013).
 *
 * A plugin's manifest may declare `ui.estimate_source` to offer priced items
 * (captured supplier quotes) as estimate lines. Historically EstimateView took
 * the FIRST such plugin (`.find()`), which made every provider after it
 * unreachable. This composable collects them ALL, so the estimate screen can
 * render one "Add {label}" button per installed provider.
 *
 * Filtering, in order:
 *  1. manifest shape — `key` + `ui.estimate_source` with both endpoints;
 *  2. endpoint namespace — both endpoints must live under
 *     `/api/plugins/<key>/`. Manifest strings are instructions to the host's
 *     AUTHENTICATED api client (same invariant as usePluginScreen's
 *     safePluginEndpoint), so a manifest may only point the host at the
 *     declaring plugin's own routes — never at core endpoints or another
 *     plugin's;
 *  3. permission — the user can `read` the plugin. UX-only (the proxy
 *     enforces server-side); gate via auth.hasPluginPermission, which mirrors
 *     the backend's blanket-OR-per-plugin resolution — testing the per-plugin
 *     key alone would hide providers from admins (PR #304 trap).
 *
 * Discovery failure (plugin-host down, no plugins) resolves to [] — the
 * feature stays invisible, exactly like stock core.
 */
import { ref } from 'vue';

/** Picker table fallback: the historical captured-door shape. A provider whose
 * rows differ (e.g. a configurator with model/size/unit_cost) declares its own
 * `estimate_source.columns: [{field, label, money?}]` in its manifest. */
export const DEFAULT_PICKER_COLUMNS = [
  { field: 'qcd', label: 'Quote #', money: false },
  { field: 'cart_name', label: 'Cart', money: false },
  { field: 'price', label: 'Price', money: true },
];

/** Accept manifest-declared picker columns; anything malformed falls back to
 * the default so a bad manifest degrades to blank cells, never a blank screen. */
export function normalizeColumns(cols) {
  if (!Array.isArray(cols) || !cols.length) return DEFAULT_PICKER_COLUMNS;
  const clean = cols
    .filter((c) => c && typeof c.field === 'string' && c.field && typeof c.label === 'string')
    .map((c) => ({ field: c.field, label: c.label, money: !!c.money }));
  return clean.length ? clean : DEFAULT_PICKER_COLUMNS;
}

/** Map a failed list/draft fetch to a picker-renderable reason. 403 means the
 * grant is missing (fixable in Roles & Permissions); everything else — 503
 * stale-plugin fail-closed, plugin-host down, 5xx — is "unavailable". */
export function classifyPickerError(err) {
  return err?.status === 403 ? 'forbidden' : 'unavailable';
}

export function useEstimateSources(api, auth) {
  const sources = ref([]); // [{ pluginKey, label, list_endpoint, draft_endpoint, columns }]

  async function discover() {
    try {
      // Cold-load race: the estimate route has no permission meta, so the
      // router guard never awaited the permission fetch before this runs.
      // Filtering against a not-yet-loaded set silently hid every provider
      // from non-admins (admins pass via the unloaded-set escape hatch in
      // hasPermission). loadPermissions caches and single-flights, so this
      // is a no-op after the first call anywhere in the session.
      if (auth.loadPermissions) await auth.loadPermissions().catch(() => {});
      const plugins = await api.get('/api/plugins');
      sources.value = (Array.isArray(plugins) ? plugins : [])
        .filter((p) => p?.key && p?.ui?.estimate_source)
        .map((p) => ({ key: p.key, name: p.name, src: p.ui.estimate_source }))
        .filter(({ key, src }) => {
          const prefix = `/api/plugins/${key}/`;
          return (
            typeof src.list_endpoint === 'string' && src.list_endpoint.startsWith(prefix)
            && typeof src.draft_endpoint === 'string' && src.draft_endpoint.startsWith(prefix)
          );
        })
        .filter(({ key }) => auth.hasPluginPermission(key, 'read'))
        .map(({ key, name, src }) => ({
          pluginKey: key,
          label: src.label || name || key,
          list_endpoint: src.list_endpoint,
          draft_endpoint: src.draft_endpoint,
          columns: normalizeColumns(src.columns),
        }));
    } catch {
      sources.value = []; // no plugin-host / no plugins → feature stays hidden
    }
  }

  return { sources, discover };
}
