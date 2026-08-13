/**
 * useEstimateSources — multi-provider estimate_source discovery (ADR-013).
 *
 * EstimateView historically took the FIRST plugin with ui.estimate_source
 * (`.find()`), so with two providers installed (e.g. a capture plugin and a
 * configurator plugin) the second was unreachable from the estimate screen.
 * These tests pin the collect-them-all behavior and the three filters:
 * manifest shape, endpoint namespace, and user permission.
 */
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_PICKER_COLUMNS,
  classifyPickerError,
  normalizeColumns,
  useEstimateSources,
} from '../useEstimateSources';

const SRC = (key, extra = {}) => ({
  label: `${key} item`,
  list_endpoint: `/api/plugins/${key}/things`,
  draft_endpoint: `/api/plugins/${key}/things/{id}/estimate-line`,
  ...extra,
});

function harness({ catalog, canRead = () => true }) {
  const api = {
    get: async () => {
      if (catalog instanceof Error) throw catalog;
      return catalog;
    },
  };
  const auth = { hasPluginPermission: (key, action) => canRead(key, action) };
  return useEstimateSources(api, auth);
}

describe('useEstimateSources — discovery', () => {
  it('collects EVERY plugin that declares an estimate_source, in catalog order', async () => {
    const { sources, discover } = harness({
      catalog: [
        { key: 'alpha', name: 'Alpha', ui: { estimate_source: SRC('alpha') } },
        { key: 'plain', name: 'No hook', ui: {} },
        { key: 'beta', name: 'Beta', ui: { estimate_source: SRC('beta') } },
      ],
    });
    await discover();
    expect(sources.value.map((s) => s.pluginKey)).toEqual(['alpha', 'beta']);
    expect(sources.value[0]).toMatchObject({
      pluginKey: 'alpha',
      label: 'alpha item',
      list_endpoint: '/api/plugins/alpha/things',
      draft_endpoint: '/api/plugins/alpha/things/{id}/estimate-line',
    });
  });

  it('falls back to plugin name, then key, when the source has no label', async () => {
    const { sources, discover } = harness({
      catalog: [
        { key: 'a', name: 'Named', ui: { estimate_source: SRC('a', { label: '' }) } },
        { key: 'b', ui: { estimate_source: SRC('b', { label: undefined }) } },
      ],
    });
    await discover();
    expect(sources.value.map((s) => s.label)).toEqual(['Named', 'b']);
  });

  it("drops a source whose endpoints leave the plugin's own /api/plugins/<key>/ namespace", async () => {
    const { sources, discover } = harness({
      catalog: [
        // manifest strings drive the host's AUTHENTICATED api client — a
        // manifest may not point the picker at core or another plugin
        { key: 'evil', ui: { estimate_source: SRC('evil', { list_endpoint: '/api/customers' }) } },
        { key: 'crossed', ui: { estimate_source: SRC('other') } },
        { key: 'ok', ui: { estimate_source: SRC('ok') } },
      ],
    });
    await discover();
    expect(sources.value.map((s) => s.pluginKey)).toEqual(['ok']);
  });

  it('drops sources the user cannot read (blanket-OR-per-plugin is the helper\'s job)', async () => {
    const { sources, discover } = harness({
      catalog: [
        { key: 'granted', ui: { estimate_source: SRC('granted') } },
        { key: 'denied', ui: { estimate_source: SRC('denied') } },
      ],
      canRead: (key, action) => key === 'granted' && action === 'read',
    });
    await discover();
    expect(sources.value.map((s) => s.pluginKey)).toEqual(['granted']);
  });

  it('awaits the permission load before filtering (cold-load race)', async () => {
    // The estimate route has no permission meta, so nothing guarantees the
    // permission set is loaded when discovery runs. Grading against an empty
    // set hid every provider from non-admins for that page load.
    let loaded = false;
    const api = { get: async () => [{ key: 'p', ui: { estimate_source: SRC('p') } }] };
    const auth = {
      loadPermissions: async () => { loaded = true; },
      hasPluginPermission: () => loaded,
    };
    const { sources, discover } = useEstimateSources(api, auth);
    await discover();
    expect(sources.value.map((s) => s.pluginKey)).toEqual(['p']);
  });

  it('tolerates a failed permission load (server still enforces)', async () => {
    const api = { get: async () => [{ key: 'p', ui: { estimate_source: SRC('p') } }] };
    const auth = {
      loadPermissions: async () => { throw new Error('perm fetch down'); },
      hasPluginPermission: () => true,
    };
    const { sources, discover } = useEstimateSources(api, auth);
    await discover();
    expect(sources.value.map((s) => s.pluginKey)).toEqual(['p']);
  });

  it('resolves to [] when the catalog fetch fails or returns a non-array', async () => {
    const failed = harness({ catalog: new Error('plugin-host down') });
    await failed.discover();
    expect(failed.sources.value).toEqual([]);

    const weird = harness({ catalog: { detail: 'not a list' } });
    await weird.discover();
    expect(weird.sources.value).toEqual([]);
  });
});

describe('useEstimateSources — picker columns', () => {
  it('defaults to the historical captured-door shape when the manifest declares none', () => {
    expect(normalizeColumns(undefined)).toEqual(DEFAULT_PICKER_COLUMNS);
    expect(normalizeColumns([])).toEqual(DEFAULT_PICKER_COLUMNS);
  });

  it('honors manifest columns, including the money flag, and drops malformed entries', () => {
    const cols = normalizeColumns([
      { field: 'model', label: 'Model' },
      { field: 'unit_cost', label: 'Unit Cost ($)', money: true },
      { label: 'no field' },
      null,
    ]);
    expect(cols).toEqual([
      { field: 'model', label: 'Model', money: false },
      { field: 'unit_cost', label: 'Unit Cost ($)', money: true },
    ]);
  });

  it('falls back to the default when every declared column is malformed', () => {
    expect(normalizeColumns([{ nope: 1 }])).toEqual(DEFAULT_PICKER_COLUMNS);
  });

  it('is applied per-source during discovery', async () => {
    const { sources, discover } = harness({
      catalog: [
        { key: 'conf', ui: { estimate_source: SRC('conf', { columns: [{ field: 'model', label: 'Model' }] }) } },
        { key: 'cap', ui: { estimate_source: SRC('cap') } },
      ],
    });
    await discover();
    expect(sources.value[0].columns).toEqual([{ field: 'model', label: 'Model', money: false }]);
    expect(sources.value[1].columns).toEqual(DEFAULT_PICKER_COLUMNS);
  });
});

describe('classifyPickerError', () => {
  it('maps 403 to forbidden (missing grant) and everything else to unavailable', () => {
    expect(classifyPickerError({ status: 403 })).toBe('forbidden');
    expect(classifyPickerError({ status: 503 })).toBe('unavailable');
    expect(classifyPickerError({ status: 500 })).toBe('unavailable');
    expect(classifyPickerError(new Error('fetch failed'))).toBe('unavailable');
    expect(classifyPickerError(undefined)).toBe('unavailable');
  });
});
