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
/**
 * Value of a manifest-declared column for one row.
 *
 * PluginScreen takes over `<Column>`'s #body slot (to render the per-cell label
 * the phone card layout needs), which means resolving the field itself instead
 * of letting PrimeVue do it. PrimeVue's resolveFieldData supports dotted paths,
 * so this has to as well — otherwise a manifest declaring `field: "door.width"`
 * would silently render blank cells the day someone writes one.
 *
 * Exported for the unit spec.
 */
export function cellValue(row, field) {
  if (!row || typeof field !== 'string' || !field) return '';
  if (!field.includes('.')) return row[field];
  return field.split('.').reduce((acc, part) => (acc == null ? acc : acc[part]), row);
}

// Tab text per screen type when the manifest omits `title` (it is optional:
// the eventlog and cellcomms help screens ship without one).
const _TYPE_LABELS = {
  list: 'List', settings: 'Settings', upload: 'Upload', help: 'Help', browser: 'Workspace',
};

/**
 * Tab label for a manifest screen. A missing or blank `title` used to render
 * an empty tab nobody could find; fall back to the screen's type, then to its
 * position.
 *
 * Exported for the unit spec.
 */
export function screenLabel(screen, index = 0) {
  const title = typeof screen?.title === 'string' ? screen.title.trim() : '';
  if (title) return title;
  return _TYPE_LABELS[screen?.type] || `Screen ${index + 1}`;
}

/**
 * Text to show beside a form for a refused request. A 403 with no reason of
 * its own (a plugin's bare `HTTPException(403)` arrives as `Forbidden`) gets
 * the same plain sentence the api layer's toast uses. A FastAPI validation
 * 422 arrives as its stringified `detail` list (the proxy passes the plugin's
 * body through), so each entry becomes "field: msg" instead of raw JSON. Any
 * other refusal shows the server's own reason.
 *
 * Exported for PluginScreen/BrowserStream and the unit spec.
 */
export function refusalText(e, fallback) {
  const msg = typeof e?.message === 'string' ? e.message.trim() : '';
  const jsonish = /^[[{]/.test(msg);
  if (e?.status === 403 && (!msg || msg === 'Forbidden' || jsonish
      || /^Request failed \(403\)$/.test(msg))) {
    return "You don't have permission to do that.";
  }
  if (jsonish) {
    let parsed;
    try { parsed = JSON.parse(msg); } catch { return fallback; }
    const items = Array.isArray(parsed) ? parsed : [parsed];
    const lines = items
      .filter((d) => d && typeof d.msg === 'string')
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : '';
        return field && field !== 'body' ? `${field}: ${d.msg}` : d.msg;
      });
    return lines.length ? lines.join('; ') : fallback;
  }
  return msg || fallback;
}

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

  /** Re-fetch ONE list screen with a server-side search term.
   *
   * Server-side on purpose: the existing catalog screens filter client-side over
   * whatever page they happened to load, so a large feed renders its first page
   * and the search box only ever sees those rows. A plugin list that declares
   * `search` gets the real thing — the term goes to the endpoint and the plugin
   * decides what matches.
   *
   * The endpoint is re-validated through safePluginEndpoint so a manifest can
   * never point the host's authenticated fetch outside its own namespace.
   */
  async function searchList(screen, term) {
    if (!screen?.endpoint || !screen.search) return;
    // Guard returns a BOOLEAN, not the endpoint — keep the original string.
    if (!safePluginEndpoint(screen.endpoint)) {
      error.value = `plugin ${pluginKey}: refused search endpoint ${screen.endpoint}`;
      return;
    }
    const ep = screen.endpoint;
    const param = screen.search.param || 'q';
    const url = `${ep}${ep.includes('?') ? '&' : '?'}${param}=${encodeURIComponent(term || '')}`;
    loading.value = true;
    try {
      rowsByEndpoint.value = {
        ...rowsByEndpoint.value,
        [screen.endpoint]: (await api.get(url)) || [],
      };
    } catch (e) {
      error.value = e?.message || 'search failed';
    } finally {
      loading.value = false;
    }
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
  //
  // Never throws: a refused POST (422 validation, 403 permission) used to reject
  // through the form's submit handler into the ErrorBoundary and replace the
  // whole page with "We hit an error" (GDXA-1873). Returns `{ ok, message }` so
  // the form keeps what the user typed and says why on failure. The api layer
  // has already toasted the refusal.
  async function create(values, screen) {
    const target = screen && screen.type === 'list' ? screen : _lists()[0];
    const endpoint = target?.create?.endpoint;
    if (!endpoint) return { ok: false, message: 'This form has no create endpoint.' };
    try {
      await api.post(endpoint, values);
    } catch (e) {
      return { ok: false, message: refusalText(e, 'Not saved.') };
    }
    // The row is saved; a failed refresh must not report the save as failed.
    if (target?.endpoint) {
      try {
        rowsByEndpoint.value = {
          ...rowsByEndpoint.value,
          [target.endpoint]: (await api.get(target.endpoint)) || [],
        };
      } catch (e) {
        error.value = e?.message || 'refresh failed';
      }
    }
    return { ok: true };
  }

  // Upload one file to an `upload` screen's endpoint as multipart FormData
  // (field name `file` — FastAPI UploadFile's default). Same same-plugin guard
  // as every other manifest-declared endpoint, then refresh the list screens —
  // an import lands rows the tables should show without a manual reload.
  // Returns the server's summary object, or null when refused/failed (the
  // error ref carries why).
  async function uploadTo(screen, file) {
    const ep = screen?.endpoint;
    if (!file || !ep) return null;
    if (!safePluginEndpoint(ep)) {
      error.value = `plugin ${pluginKey}: refused upload endpoint ${ep}`;
      return null;
    }
    const form = new FormData();
    form.append('file', file);
    loading.value = true;
    error.value = null;
    try {
      const result = await api.post(ep, form);
      const next = { ...rowsByEndpoint.value };
      for (const s of _lists()) {
        next[s.endpoint] = (await api.get(s.endpoint)) || [];
      }
      rowsByEndpoint.value = next;
      return result;
    } catch (e) {
      error.value = e?.message || 'upload failed';
      return null;
    } finally {
      loading.value = false;
    }
  }

  // POST (no body) to a screen-declared action endpoint, e.g. the upload
  // screen's optional `secondary_action`. Same guard, same list refresh.
  async function runAction(endpoint) {
    if (!safePluginEndpoint(endpoint)) {
      error.value = `plugin ${pluginKey}: refused action endpoint ${endpoint}`;
      return null;
    }
    loading.value = true;
    error.value = null;
    try {
      const result = await api.post(endpoint, {});
      const next = { ...rowsByEndpoint.value };
      for (const s of _lists()) {
        next[s.endpoint] = (await api.get(s.endpoint)) || [];
      }
      rowsByEndpoint.value = next;
      return result;
    } catch (e) {
      error.value = e?.message || 'action failed';
      return null;
    } finally {
      loading.value = false;
    }
  }

  return {
    screens, rows, rowsFor, loading, error, load, create, searchList,
    fetchOptions, safePluginEndpoint, interpolateEndpoint, uploadTo, runAction,
  };
}
