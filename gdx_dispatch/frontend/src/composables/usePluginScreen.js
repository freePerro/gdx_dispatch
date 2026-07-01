import { ref, computed } from 'vue';

/**
 * usePluginScreen — host-side renderer logic for a plugin's UI manifest
 * (ADR-013 step 4). Fetches GET /api/plugins/<key>/ui (via the core proxy),
 * then loads the list screen's data. No PrimeVue/DOM here so it unit-tests
 * cleanly; PluginScreen.vue is a thin template over this.
 *
 * @param {string} pluginKey
 * @param {{get: Function, post: Function}} api  e.g. useApiWithToast()
 */
export function usePluginScreen(pluginKey, api) {
  const screens = ref([]);
  // Rows PER list screen, keyed by the screen's endpoint — a plugin may declare
  // several list screens (e.g. a data table + an editable-settings table), and
  // each must show/refresh its own data instead of sharing one array.
  const rowsByEndpoint = ref({});
  const loading = ref(false);
  const error = ref(null);

  function _lists() {
    return (screens.value || []).filter((s) => s.type === 'list' && s.endpoint);
  }

  // Back-compat: `rows` is the FIRST list screen's rows (older single-list
  // callers/tests read this directly).
  const rows = computed(() => {
    const first = _lists()[0];
    return first ? (rowsByEndpoint.value[first.endpoint] || []) : [];
  });

  function rowsFor(screen) {
    return (screen?.endpoint && rowsByEndpoint.value[screen.endpoint]) || [];
  }

  // ── option sources for select fields ─────────────────────────────────────
  // A field's options come from a plugin-declared `options_endpoint`. Guards
  // (all here, so PluginScreen.vue carries no security logic):
  //  1. same-plugin only — refuse any endpoint not under /api/plugins/<key>/,
  //     so a manifest can't point the host's authed fetch at another namespace
  //     or an absolute URL.
  //  2. interpolate {field} bindings URL-ENCODED (no param/path injection).
  //  3. race-guard — a stale response for a dependent field is dropped.
  const _optSeq = {};

  function safePluginEndpoint(ep) {
    return typeof ep === 'string' && ep.startsWith(`/api/plugins/${pluginKey}/`);
  }

  function interpolateEndpoint(ep, values) {
    return ep.replace(/\{(\w+)\}/g, (_, k) => encodeURIComponent(values?.[k] ?? ''));
  }

  // Returns an options array, or null if this response was superseded (caller
  // must ignore null so it doesn't overwrite fresher options).
  async function fetchOptions(field, values) {
    const raw = field?.options_endpoint;
    if (!raw) return field?.options || [];        // static options, or none
    if (!safePluginEndpoint(raw)) {
      error.value = `plugin ${pluginKey}: refused options endpoint ${raw}`;
      return [];
    }
    const url = interpolateEndpoint(raw, values);
    const seq = (_optSeq[field.name] = (_optSeq[field.name] || 0) + 1);
    let data;
    try {
      data = await api.get(url);
    } catch {
      data = [];
    }
    if (seq !== _optSeq[field.name]) return null;  // superseded — drop it
    return Array.isArray(data) ? data : [];
  }

  async function load() {
    loading.value = true;
    error.value = null;
    try {
      const manifest = await api.get(`/api/plugins/${pluginKey}/ui`);
      screens.value = manifest?.screens || [];
      const next = {};
      for (const s of _lists()) {
        next[s.endpoint] = (await api.get(s.endpoint)) || [];
      }
      rowsByEndpoint.value = next;
    } catch (e) {
      error.value = e?.message || 'failed to load plugin';
    } finally {
      loading.value = false;
    }
  }

  // Create against a SPECIFIC screen's create endpoint (defaults to the first
  // list for back-compat), then refresh just that screen's rows.
  async function create(values, screen) {
    const target = screen && screen.type === 'list' ? screen : _lists()[0];
    const endpoint = target?.create?.endpoint;
    if (!endpoint) return;
    await api.post(endpoint, values);
    if (target?.endpoint) {
      rowsByEndpoint.value = {
        ...rowsByEndpoint.value,
        [target.endpoint]: (await api.get(target.endpoint)) || [],
      };
    }
  }

  return {
    screens, rows, rowsFor, loading, error, load, create,
    fetchOptions, safePluginEndpoint, interpolateEndpoint,
  };
}
