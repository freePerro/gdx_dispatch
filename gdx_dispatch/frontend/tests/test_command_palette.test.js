import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { flushPromises, mount } from '@vue/test-utils';
import { createRouter, createMemoryHistory } from 'vue-router';
import { defineComponent } from 'vue';
import App from '../src/App.vue';
import CommandPalette from '../src/components/CommandPalette.vue';

vi.mock('../src/composables/useTenantModules', () => ({
  useTenantModules: () => ({
    allEnabledModules: {
      value: [
        { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', type: 'Jobs', to: '/jobs' },
        { key: 'customers', label: 'Customers', icon: 'pi pi-users', type: 'Customers', to: '/customers' },
        { key: 'billing', label: 'Billing', icon: 'pi pi-dollar', type: 'Invoices', to: '/billing' },
      ],
    },
    isEnabled: () => true,
    loadTenantModules: vi.fn(),
  }),
}));

const EmptyView = defineComponent({ template: '<div />' });

function createTestRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: EmptyView },
      { path: '/jobs', component: EmptyView },
      { path: '/customers', component: EmptyView },
      { path: '/billing', component: EmptyView },
    ],
  });
}

describe('command palette', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it('opens from global Ctrl+K shortcut in App', async () => {
    const router = createTestRouter();
    await router.push('/');
    await router.isReady();

    const wrapper = mount(App, {
      global: {
        plugins: [createPinia(), router],
        stubs: {
          ThemeProvider: defineComponent({ template: '<div><slot /></div>' }),
          // AppLayout-into-App.vue refactor: App.vue now mounts AppLayout
          // (sidebar+topbar+bottom-nav) above <router-view>. Stub it here
          // so this test stays focused on the Ctrl+K shortcut wiring and
          // doesn't have to bring up the full sidebar/store graph.
          AppLayout: defineComponent({ template: '<div><slot /></div>' }),
          'router-view': defineComponent({ template: '<div />' }),
          CommandPalette: defineComponent({
            props: ['modelValue'],
            template: '<div class="palette-state">{{ modelValue }}</div>',
          }),
        },
      },
    });

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
    await wrapper.vm.$nextTick();

    expect(wrapper.find('.palette-state').text()).toContain('true');
  });

  it('searches and navigates on Enter', async () => {
    const router = createTestRouter();
    await router.push('/');
    await router.isReady();

    const wrapper = mount(CommandPalette, {
      props: { modelValue: true },
      global: {
        plugins: [router],
        stubs: {
          Dialog: defineComponent({
            props: ['visible'],
            emits: ['update:visible', 'hide'],
            template: '<div v-if="visible" class="dialog"><slot name="header" /><slot /></div>',
          }),
          InputText: defineComponent({
            props: ['modelValue'],
            emits: ['update:modelValue'],
            template:
              '<input class="palette-input" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
          }),
        },
      },
    });

    await wrapper.find('.palette-input').setValue('customer');
    expect(wrapper.text()).toContain('Customers');

    await wrapper.find('.palette-body').trigger('keydown', { key: 'Enter' });
    await flushPromises();

    expect(router.currentRoute.value.path).toBe('/customers');
  });
});
