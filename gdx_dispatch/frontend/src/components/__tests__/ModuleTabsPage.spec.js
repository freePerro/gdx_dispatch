/**
 * ModuleTabsPage — 2026-07-07 tabbed-pages.
 *
 * The shared tab-bar layout for nav clusters. Pins:
 *   - renders one tab per visible cluster child, in catalog order,
 *     using the short `tabLabel` caption
 *   - marks the tab matching the current route as active/aria-current
 *   - hides the whole bar when fewer than two tabs survive filtering
 *     (a one-tab bar is noise; the child view already has its header)
 *   - visibility comes from useTenantModules.allEnabledModules, so tabs
 *     automatically mirror sidebar enablement + permission filtering
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ref } from 'vue';
import { mount, RouterLinkStub } from '@vue/test-utils';

const modules = ref([]);

vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ allEnabledModules: modules }),
}));

const route = { path: '/phone-com/messages' };
vi.mock('vue-router', () => ({
  useRoute: () => route,
}));

import ModuleTabsPage from '../ModuleTabsPage.vue';

const PHONE_MODULES = [
  { key: 'phone_com_calls', label: 'Phone.com Calls', tabLabel: 'Calls', icon: 'pi pi-phone', to: '/phone-com/calls', cluster: 'phone_hub' },
  { key: 'phone_com_messages', label: 'Phone.com SMS', tabLabel: 'SMS', icon: 'pi pi-comment', to: '/phone-com/messages', cluster: 'phone_hub' },
  { key: 'phone_com_faxes', label: 'Phone.com Faxes', tabLabel: 'Faxes', icon: 'pi pi-file-pdf', to: '/phone-com/faxes', cluster: 'phone_hub' },
  { key: 'inbox', label: 'Inbox', icon: 'pi pi-inbox', to: '/inbox' },
];

function mountPage(clusterKey = 'phone_hub') {
  return mount(ModuleTabsPage, {
    props: { clusterKey },
    global: {
      stubs: { RouterLink: RouterLinkStub, RouterView: true },
    },
  });
}

beforeEach(() => {
  modules.value = PHONE_MODULES;
  route.path = '/phone-com/messages';
});

describe('ModuleTabsPage', () => {
  it('renders one tab per visible cluster child with the short caption', () => {
    const wrapper = mountPage();
    const tabs = wrapper.findAllComponents(RouterLinkStub);
    expect(tabs.map((t) => t.props('to'))).toEqual([
      '/phone-com/calls',
      '/phone-com/messages',
      '/phone-com/faxes',
    ]);
    expect(wrapper.text()).toContain('SMS');
    // Short captions, not the sidebar-length labels.
    expect(wrapper.text()).not.toContain('Phone.com SMS');
    // Non-cluster modules (inbox) never leak in.
    expect(wrapper.find('[data-testid="module-tab-inbox"]').exists()).toBe(false);
  });

  it('marks the current route tab active', () => {
    const wrapper = mountPage();
    const active = wrapper.find('[data-testid="module-tab-phone_com_messages"]');
    expect(active.classes()).toContain('active');
    expect(active.attributes('aria-current')).toBe('page');
    expect(
      wrapper.find('[data-testid="module-tab-phone_com_calls"]').classes()
    ).not.toContain('active');
  });

  it('hides the tab bar when fewer than two tabs survive filtering', () => {
    modules.value = [PHONE_MODULES[0]];
    const wrapper = mountPage();
    expect(wrapper.find('[data-testid="module-tab-bar"]').exists()).toBe(false);
    // The child route still renders.
    expect(wrapper.findComponent({ name: 'RouterView' }).exists()).toBe(true);
  });
});
