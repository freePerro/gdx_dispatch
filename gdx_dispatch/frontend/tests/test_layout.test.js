import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount, shallowMount } from '@vue/test-utils';
import { createRouter, createMemoryHistory } from 'vue-router';
import { defineComponent } from 'vue';
import AppSidebar from '../src/components/AppSidebar.vue';
import AppLayout from '../src/components/AppLayout.vue';

vi.mock('../src/composables/useTenantModules', () => ({
  useTenantModules: () => ({
    categories: {
      value: [
        {
          key: 'operations',
          label: 'Operations',
          modules: [
            { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/jobs' },
            { key: 'dispatch', label: 'Dispatch', icon: 'pi pi-map', to: '/dispatch' },
          ],
        },
        {
          key: 'customers',
          label: 'Customers',
          modules: [{ key: 'customers', label: 'Customers', icon: 'pi pi-users', to: '/customers' }],
        },
      ],
    },
    allEnabledModules: {
      value: [
        { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/jobs' },
        { key: 'dispatch', label: 'Dispatch', icon: 'pi pi-map', to: '/dispatch' },
        { key: 'customers', label: 'Customers', icon: 'pi pi-users', to: '/customers' },
      ],
    },
    isEnabled: () => true,
    loadTenantModules: vi.fn(),
  }),
}));

const PanelMenuStub = defineComponent({
  props: ['model'],
  template: `
    <div class="panel-menu">
      <div v-for="group in model" :key="group.key" class="group">
        <div class="group-label">{{ group.label }}</div>
        <template v-for="item in group.items" :key="item.key">
          <slot name="item" :item="item" />
        </template>
      </div>
    </div>
  `,
});

const ButtonStub = defineComponent({
  emits: ['click'],
  template: '<button type="button" @click="$emit(\'click\')"><slot /></button>',
});

const EmptyView = defineComponent({ template: '<div />' });

function createTestRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/jobs', component: EmptyView },
      { path: '/dispatch', component: EmptyView },
      { path: '/customers', component: EmptyView },
      { path: '/dashboard', component: EmptyView },
    ],
  });
}

describe('layout shell', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it('sidebar renders module groups and items', async () => {
    const router = createTestRouter();
    await router.push('/jobs');
    await router.isReady();

    const wrapper = mount(AppSidebar, {
      global: {
        plugins: [router],
        stubs: {
          PanelMenu: PanelMenuStub,
          Button: ButtonStub,
        },
      },
    });

    expect(wrapper.text()).toContain('Operations');
    expect(wrapper.text()).toContain('Customers');
    expect(wrapper.text()).toContain('Jobs');
  });

  it('layout collapses sidebar when topbar toggles navigation', async () => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1280 });

    const router = createTestRouter();
    await router.push('/jobs');
    await router.isReady();

    const wrapper = shallowMount(AppLayout, {
      global: {
        plugins: [router],
        stubs: {
          AppSidebar: defineComponent({ template: '<aside class="app-sidebar" />' }),
          AppTopbar: defineComponent({
            emits: ['toggle-navigation'],
            template: '<button class="nav-toggle" @click="$emit(\'toggle-navigation\')">Toggle</button>',
          }),
          AppBottomNav: defineComponent({ template: '<nav class="bottom-nav" />' }),
          Breadcrumb: defineComponent({ template: '<div class="breadcrumb" />' }),
          Drawer: defineComponent({ props: ['visible'], template: '<div v-if="visible"><slot /></div>' }),
        },
      },
    });

    await wrapper.find('.nav-toggle').trigger('click');

    expect(wrapper.classes()).toContain('collapsed');
  });

  it('mobile layout hides desktop sidebar and shows bottom nav', async () => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 640 });

    const router = createTestRouter();
    await router.push('/jobs');
    await router.isReady();

    const wrapper = shallowMount(AppLayout, {
      global: {
        plugins: [router],
        stubs: {
          AppSidebar: defineComponent({ template: '<aside class="app-sidebar" />' }),
          AppTopbar: defineComponent({ template: '<div class="topbar" />' }),
          AppBottomNav: defineComponent({ template: '<nav class="bottom-nav" />' }),
          Breadcrumb: defineComponent({ template: '<div class="breadcrumb" />' }),
          Drawer: defineComponent({ props: ['visible'], template: '<div v-if="visible"><slot /></div>' }),
        },
      },
    });
    window.dispatchEvent(new Event('resize'));
    await wrapper.vm.$nextTick();

    expect(wrapper.find('.app-sidebar').exists()).toBe(false);
    expect(wrapper.find('.bottom-nav').exists()).toBe(true);
  });
});
