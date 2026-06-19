import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia, createPinia } from 'pinia';
import GoogleMapsIntegrationCard from '../src/components/GoogleMapsIntegrationCard.vue';

const getMock = vi.fn();
const patchMock = vi.fn();

vi.mock('../src/composables/useApi', () => ({
  useApi: () => ({
    get: getMock,
    patch: patchMock,
    request: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
  }),
}));

describe('GoogleMapsIntegrationCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    patchMock.mockReset();
  });

  it('shows "Not Configured" badge when key is empty', async () => {
    getMock.mockResolvedValueOnce({ key: '', configured: false });
    const wrapper = mount(GoogleMapsIntegrationCard);
    await flushPromises();
    expect(wrapper.text()).toContain('Not Configured');
    expect(wrapper.find('[data-testid="maps-key-input"]').element.value).toBe('');
  });

  it('shows "Configured" badge and prefills the input when a key exists', async () => {
    getMock.mockResolvedValueOnce({ key: 'AIzaTEST123', configured: true });
    const wrapper = mount(GoogleMapsIntegrationCard);
    await flushPromises();
    expect(wrapper.text()).toContain('Configured');
    expect(wrapper.find('[data-testid="maps-key-input"]').element.value).toBe('AIzaTEST123');
  });

  it('PATCHes the key when Save is clicked and surfaces success', async () => {
    getMock.mockResolvedValueOnce({ key: '', configured: false });
    patchMock.mockResolvedValueOnce({ configured: true });
    const wrapper = mount(GoogleMapsIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-testid="maps-key-input"]').setValue('AIzaNEW999');
    await wrapper.find('[data-testid="maps-key-save"]').trigger('click');
    await flushPromises();

    expect(patchMock).toHaveBeenCalledWith('/api/settings/integrations/google-maps', {
      key: 'AIzaNEW999',
    });
    expect(wrapper.text()).toContain('Key saved.');
  });

  it('toggles password visibility on Show/Hide click', async () => {
    getMock.mockResolvedValueOnce({ key: 'AIza', configured: true });
    const wrapper = mount(GoogleMapsIntegrationCard);
    await flushPromises();

    const input = wrapper.find('[data-testid="maps-key-input"]');
    expect(input.attributes('type')).toBe('password');

    await wrapper.find('[data-testid="maps-key-toggle-visibility"]').trigger('click');
    expect(wrapper.find('[data-testid="maps-key-input"]').attributes('type')).toBe('text');
  });
});
