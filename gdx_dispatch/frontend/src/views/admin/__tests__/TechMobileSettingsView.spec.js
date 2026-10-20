/**
 * The Customer Alert Tags tab must not claim "No tags yet" before its fetch
 * has even started: onMounted runs loadTags() only after `await load()`
 * (GDXA-941).
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import TechMobileSettingsView from '../TechMobileSettingsView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
const apiGetMock = vi.fn();
const apiDelMock = vi.fn();
vi.mock('../../../composables/useApi', () => ({
  useApi: () => ({ get: apiGetMock, put: vi.fn(), post: vi.fn(), patch: vi.fn(), del: apiDelMock }),
}));
const confirmAsync = vi.fn();
vi.mock('../../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync, confirmDestructive: vi.fn() }),
}));

// Every panel renders, so the tags panel is in the DOM whatever tab is active.
const pass = { template: '<div><slot /></div>' };
const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: pass, TabList: pass, Tab: pass, TabPanels: pass, TabPanel: pass,
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
  Dialog: { template: '<div />' },
};

describe('TechMobileSettingsView tags tab (GDXA-941)', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
  });

  it('shows Loading tags, not "No tags yet", while the settings request is in flight', async () => {
    let resolveSettings;
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/admin/feature-settings/tech-mobile') {
        return new Promise((r) => { resolveSettings = r; });
      }
      if (url === '/api/admin/customer-tags') return Promise.resolve({ tags: [] });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });

    const w = mount(TechMobileSettingsView, { global: { stubs } });
    await flushPromises();

    expect(apiGetMock.mock.calls.map((c) => c[0])).not.toContain('/api/admin/customer-tags');
    expect(w.text()).toContain('Loading tags…');
    expect(w.text()).not.toContain('No tags yet');

    resolveSettings({ catalog: [], overrides: {}, resolved: {} });
    await flushPromises();

    expect(w.text()).not.toContain('Loading tags…');
    expect(w.text()).toContain('No tags yet');
  });
});

// Delete called confirmAsync with no import: a ReferenceError in the click
// handler, so the button did nothing (GDXA-1690).
describe('TechMobileSettingsView tag delete (GDXA-1690)', () => {
  const tag = { id: 't1', name: 'Dog on site', color: '#ff0000', description: '' };

  beforeEach(() => {
    apiGetMock.mockReset();
    apiDelMock.mockReset();
    confirmAsync.mockReset();
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/admin/feature-settings/tech-mobile') {
        return Promise.resolve({ catalog: [], overrides: {}, resolved: {} });
      }
      if (url === '/api/admin/customer-tags') return Promise.resolve({ tags: [tag] });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    apiDelMock.mockResolvedValue({ assignments_removed: 0 });
  });

  async function clickDelete() {
    const w = mount(TechMobileSettingsView, { global: { stubs } });
    await flushPromises();
    await w.find('button[aria-label="Delete"]').trigger('click');
    await flushPromises();
    return w;
  }

  it('asks for confirmation and fires DELETE when confirmed', async () => {
    confirmAsync.mockResolvedValue(true);
    await clickDelete();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(confirmAsync.mock.calls[0][0].message).toContain('Dog on site');
    expect(apiDelMock).toHaveBeenCalledWith('/api/admin/customer-tags/t1');
  });

  it('does not fire DELETE when the confirmation is declined', async () => {
    confirmAsync.mockResolvedValue(false);
    await clickDelete();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(apiDelMock).not.toHaveBeenCalled();
  });
});
