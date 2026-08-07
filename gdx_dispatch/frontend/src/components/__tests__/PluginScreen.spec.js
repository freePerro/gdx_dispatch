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

const SETTINGS_MANIFEST = {
  screens: [
    { type: 'settings', title: 'Settings', endpoint: '/api/plugins/example/settings' },
  ],
};

// Settings payload with the optional `ordered` group (an ordered field list the
// operator can reorder/add/remove; PUT round-trips it as `ordered`).
const SETTINGS = {
  fields: [
    { name: 'Alpha', on_quote: true },
    { name: 'Beta', on_quote: false },
    { name: 'Gamma', on_quote: false },
  ],
  ordered: {
    title: 'Line description',
    hint: 'Fields joined in this order.',
    selected: ['Alpha', 'Beta'],
    candidates: ['Alpha', 'Beta', 'Gamma'],
  },
};

const apiMock = vi.hoisted(() => ({ manifest: null, get: null, put: null, post: null }));
apiMock.get = vi.fn(async (url) => {
  if (url.endsWith('/ui')) return apiMock.manifest;
  if (url.endsWith('/settings')) return SETTINGS;
  return [{ id: 1, name: 'Spring Kit' }];
});
apiMock.put = vi.fn(async () => ({}));
apiMock.post = vi.fn(async () => ({}));

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => apiMock,
}));

// eslint-disable-next-line import/first
import PluginScreen from '../PluginScreen.vue';

const stubs = {
  DataTable: true, Column: true, InputText: true, Button: true, Checkbox: true, Select: true,
};

describe('PluginScreen.vue', () => {
  it('compiles, loads the manifest, and renders the screen + create form', async () => {
    apiMock.manifest = MANIFEST;
    const wrapper = mount(PluginScreen, { props: { pluginKey: 'example' }, global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="plugin-screen"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Example Items');
    expect(wrapper.find('form.plugin-screen__create').exists()).toBe(true);
  });

  it('renders the ordered settings group and PUTs the edited order on save', async () => {
    apiMock.manifest = SETTINGS_MANIFEST;
    const wrapper = mount(PluginScreen, { props: { pluginKey: 'example' }, global: { stubs } });
    await flushPromises();

    const names = () => wrapper.findAll('.plugin-screen__ordered-name').map((n) => n.text());
    expect(wrapper.text()).toContain('Line description');
    expect(names()).toEqual(['1. Alpha', '2. Beta']);

    await wrapper.find('[aria-label="Move Beta up"]').trigger('click');
    expect(names()).toEqual(['1. Beta', '2. Alpha']);

    // Append a candidate via the add-select (v-model then change, like PrimeVue).
    const add = wrapper.findComponent('.plugin-screen__ordered-add');
    add.vm.$emit('update:modelValue', 'Gamma');
    add.vm.$emit('change');
    await flushPromises();
    expect(names()).toEqual(['1. Beta', '2. Alpha', '3. Gamma']);

    await wrapper.find('[aria-label="Remove Alpha"]').trigger('click');
    expect(names()).toEqual(['1. Beta', '2. Gamma']);

    await wrapper.find('[label="Save"]').trigger('click');
    await flushPromises();
    expect(apiMock.put).toHaveBeenCalledWith(
      '/api/plugins/example/settings',
      { fields: ['Alpha'], ordered: ['Beta', 'Gamma'] },
      expect.anything(),
    );
  });
});
