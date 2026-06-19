/**
 * MobileEstimatesView — pin the "+ New" affordance contract.
 *
 * 2026-05-10 (Doug): "in mobile there is no way for anyone to add a new
 * job or estimate." The fix added a header "+ New" button gated by
 * hasPermission('estimates.write'). On tap, navigate to /estimates/new
 * (EstimateView's create route — already mobile-responsive at
 * @media (max-width: 768px)).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const push = vi.fn();
const hasPermission = vi.fn(() => true);

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: vi.fn(), put: vi.fn(), del: vi.fn() }),
}));
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission,
    permissions: { value: ['estimates.write'] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn().mockResolvedValue(true) }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

import MobileEstimatesView from '../MobileEstimatesView.vue';

const stubs = {
  SelectButton: { props: ['modelValue', 'options'], template: '<div />' },
  Tag: { props: ['value', 'severity'], template: '<span>{{ value }}</span>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'text', 'size', 'disabled'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
};

describe('MobileEstimatesView "+ New" affordance', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiGet.mockResolvedValue([]);
    push.mockReset();
    hasPermission.mockReset();
    hasPermission.mockReturnValue(true);
  });

  it('renders "+ New" when user has estimates.write', async () => {
    const wrapper = mount(MobileEstimatesView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-testid="mobile-estimates-new-btn"]').exists()).toBe(true);
  });

  it('hides "+ New" when user lacks estimates.write', async () => {
    hasPermission.mockImplementation((k) => k !== 'estimates.write');

    const wrapper = mount(MobileEstimatesView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-testid="mobile-estimates-new-btn"]').exists()).toBe(false);
  });

  it('routes to /estimates/new on tap', async () => {
    const wrapper = mount(MobileEstimatesView, { global: { stubs } });
    await flushPromises();

    await wrapper.find('[data-testid="mobile-estimates-new-btn"]').trigger('click');

    expect(push).toHaveBeenCalledWith('/estimates/new');
  });
});
