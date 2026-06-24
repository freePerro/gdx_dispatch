import { ref } from 'vue';

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
  const rows = ref([]);
  const loading = ref(false);
  const error = ref(null);

  function _list() {
    return screens.value.find((s) => s.type === 'list') || null;
  }

  async function load() {
    loading.value = true;
    error.value = null;
    try {
      const manifest = await api.get(`/api/plugins/${pluginKey}/ui`);
      screens.value = manifest?.screens || [];
      const list = _list();
      rows.value = list?.endpoint ? (await api.get(list.endpoint)) || [] : [];
    } catch (e) {
      error.value = e?.message || 'failed to load plugin';
    } finally {
      loading.value = false;
    }
  }

  async function create(values) {
    const list = _list();
    const endpoint = list?.create?.endpoint;
    if (!endpoint) return;
    await api.post(endpoint, values);
    if (list?.endpoint) rows.value = (await api.get(list.endpoint)) || [];
  }

  return { screens, rows, loading, error, load, create };
}
