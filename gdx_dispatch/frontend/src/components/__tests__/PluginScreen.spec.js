import { describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const MANIFEST = {
  screens: [
    {
      type: 'list',
      title: 'Example Items',
      endpoint: '/api/plugins/example/items',
      columns: [
        { field: 'id', label: 'ID' },
        { field: 'name', label: 'Name' },
      ],
      create: {
        endpoint: '/api/plugins/example/items',
        fields: [{ name: 'name', label: 'Name', type: 'text', required: true }],
      },
    },
  ],
};

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: vi.fn(async (url) => (url.endsWith('/ui') ? MANIFEST : [{ id: 1, name: 'Spring Kit' }])),
    post: vi.fn(async () => ({})),
  }),
}));

// eslint-disable-next-line import/first
import PluginScreen from '../PluginScreen.vue';

const stubs = { DataTable: true, Column: true, InputText: true, Button: true };

describe('PluginScreen.vue', () => {
  it('compiles, loads the manifest, and renders the screen + create form', async () => {
    const wrapper = mount(PluginScreen, { props: { pluginKey: 'example' }, global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="plugin-screen"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Example Items');
    expect(wrapper.find('form.plugin-screen__create').exists()).toBe(true);
  });
});
