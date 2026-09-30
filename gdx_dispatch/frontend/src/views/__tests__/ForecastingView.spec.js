/**
 * ForecastingView — recurring card pin:
 *  Recurring payments going out are shown as an outflow beside the
 *  revenue total, never inside it.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const projection = ref(null);

vi.mock('../../composables/useForecasting', () => ({
  useForecasting: () => ({
    projection,
    projectionLoading: ref(false),
    projectionError: ref(null),
    recurring: ref([]),
    recurringLoading: ref(false),
    recurringError: ref(null),
    settings: ref(null),
    settingsLoading: ref(false),
    settingsSaving: ref(false),
    loadProjection: vi.fn(),
    loadRecurring: vi.fn(),
    loadSettings: vi.fn(),
    setWindow: vi.fn(),
    syncRecurring: vi.fn(),
    saveSettings: vi.fn(),
  }),
}));
vi.mock('../../composables/useRecurringStreams', () => ({
  useRecurringStreams: () => ({
    streams: ref([]),
    loading: ref(false),
    error: ref(null),
    list: vi.fn(),
    detectNow: vi.fn(),
    confirm: vi.fn(),
  }),
}));

import ForecastingView from '../ForecastingView.vue';

function envelope(recurring) {
  return {
    window_days: 30,
    expected_total: 0,
    open_ar: { open_total: 0, expected_total: 0, by_bucket: {} },
    scheduled_jobs: { job_count: 0, jobs: [] },
    recurring: {
      count: 0,
      expected_total: 0,
      inflow_total: 0,
      outflow_total: 0,
      items: [],
      qbo_overridden: 0,
      sources: { observed: { count: 0 }, qbo_templates: { count: 0 } },
      ...recurring,
    },
  };
}

async function render(recurring) {
  projection.value = envelope(recurring);
  const wrapper = mount(ForecastingView, {
    shallow: true,
    global: { mocks: { $router: { push: vi.fn() } } },
  });
  await flushPromises();
  return wrapper;
}

describe('ForecastingView recurring card', () => {
  it('shows recurring payments as money going out, not revenue', async () => {
    const w = await render({ count: 4, outflow_total: 500.51 });
    const line = w.find('[data-testid="recurring-outflow"]');
    expect(line.exists()).toBe(true);
    expect(line.text()).toContain('$500.51');
    expect(line.text()).toContain('not counted as revenue');
  });

  it('shows no outflow line when nothing recurring is going out', async () => {
    const w = await render({ count: 0, outflow_total: 0 });
    expect(w.find('[data-testid="recurring-outflow"]').exists()).toBe(false);
  });
});
