/**
 * MobileJobsView — pin the "+ New Job" affordance contract.
 *
 * 2026-05-10: a mobile-shaped MobileJobNewDialog was added so techs can
 * create jobs (with a new customer and parts) without leaving /mobile/jobs.
 * Tap the button → dialog opens INLINE; no router-push to desktop /jobs.
 *
 * Gate: any user with hasPermission('jobs.write') — techs included. The
 * prior tech-exclusion was a workaround for the previous flow (route to
 * desktop /jobs?new=1, which the router redirects back to /mobile/jobs
 * for techs). With the inline dialog, no redirect involved.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const hasPermission = vi.fn(() => true);

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission,
    permissions: { value: ['jobs.write'] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));

import MobileJobsView from '../MobileJobsView.vue';

const stubs = {
  SelectButton: { props: ['modelValue', 'options'], template: '<div />' },
  Button: {
    props: ['label', 'icon', 'size'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  MobileJobNewDialog: {
    name: 'MobileJobNewDialog',
    props: ['visible'],
    emits: ['update:visible', 'created'],
    template: '<div data-testid="mjn-stub" :data-visible="visible" />',
  },
};

describe('MobileJobsView "+ New Job" affordance', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiGet.mockResolvedValue({ jobs: [] });
    hasPermission.mockReset();
    hasPermission.mockReturnValue(true);
  });

  it('renders "+ New" for any role with jobs.write (including tech)', async () => {
    const wrapper = mount(MobileJobsView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-testid="mobile-jobs-new-btn"]').exists()).toBe(true);
  });

  it('hides "+ New" when the user lacks jobs.write', async () => {
    hasPermission.mockImplementation((k) => k !== 'jobs.write');

    const wrapper = mount(MobileJobsView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-testid="mobile-jobs-new-btn"]').exists()).toBe(false);
  });

  it('opens MobileJobNewDialog inline on tap (no router-push)', async () => {
    const wrapper = mount(MobileJobsView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-testid="mjn-stub"]').attributes('data-visible')).toBe('false');

    await wrapper.find('[data-testid="mobile-jobs-new-btn"]').trigger('click');

    expect(wrapper.find('[data-testid="mjn-stub"]').attributes('data-visible')).toBe('true');
  });

  it('reloads the job list when the dialog emits "created"', async () => {
    const wrapper = mount(MobileJobsView, { global: { stubs } });
    await flushPromises();
    apiGet.mockClear();

    const dialog = wrapper.findComponent({ name: 'MobileJobNewDialog' });
    if (dialog.exists()) {
      await dialog.vm.$emit('created', { id: 'job-123' });
    } else {
      // Fallback for stubs that don't expose the component instance — find
      // by data-testid and dispatch a custom event would not propagate to
      // the parent's @created handler, so stick with the component-vm path.
      const stub = wrapper.find('[data-testid="mjn-stub"]');
      stub.element.dispatchEvent(new CustomEvent('created'));
    }
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/mobile/jobs');
  });

  // ─── Company-wide scope (2026-07-22) ──────────────────────────────────

  it('hides the scope switch when the server says all_jobs_enabled=false', async () => {
    apiGet.mockResolvedValue({ jobs: [], all_jobs_enabled: false });
    const wrapper = mount(MobileJobsView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="mobile-jobs-scope"]').exists()).toBe(false);
  });

  it('shows the switch when enabled and fetches ?scope=company on flip', async () => {
    apiGet.mockResolvedValue({ jobs: [], all_jobs_enabled: true });
    const wrapper = mount(MobileJobsView, { global: { stubs: scopeStubs } });
    await flushPromises();

    const row = wrapper.find('[data-testid="mobile-jobs-scope"]');
    expect(row.exists()).toBe(true);

    apiGet.mockClear();
    apiGet.mockResolvedValue({
      jobs: [{ id: 'j1', title: 'X', dispatch_status: 'assigned', assigned_tech_name: 'Bob Tech' }],
      all_jobs_enabled: true,
      scope: 'company',
    });
    await row.find('[data-value="company"]').trigger('click');
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/mobile/jobs?scope=company');
    // Company scope shows whose job each card is.
    expect(wrapper.find('[data-testid="mobile-job-tech"]').text()).toContain('Bob Tech');
    // Header flips to make the wider scope obvious.
    expect(wrapper.find('h1').text()).toBe('All Jobs');
  });
});

// SelectButton stub that actually emits selections, for the scope tests.
const scopeStubs = {
  ...stubs,
  SelectButton: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue', 'allowEmpty'],
    emits: ['update:modelValue'],
    template: `<div>
      <button
        v-for="o in options"
        :key="o.value"
        :data-value="o.value"
        @click="$emit('update:modelValue', o.value)"
      >{{ o.label }}</button>
    </div>`,
  },
};
