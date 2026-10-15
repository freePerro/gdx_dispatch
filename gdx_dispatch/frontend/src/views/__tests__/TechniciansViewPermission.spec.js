/**
 * TechniciansView — writes follow users.write (GDXA-981).
 *
 * Every technician write (create, edit, delete) takes users.write on the
 * server. An office user without it (dispatcher, sales, accounting, viewer)
 * sees the list read-only instead of a Save that 403s.
 *
 * jsdom applies no media queries — this proves wiring, never layout.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
const hasPermission = vi.fn();
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission,
    permissions: { value: [] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));

import TechniciansView from '../TechniciansView.vue';

const TECHS = [{ id: 't1', user_id: 'u1', name: 'Terry Tech', skills: [], hourly_rate: 40, active: true }];

function mountView() {
  return mount(TechniciansView, {
    global: { plugins: [PrimeVue], directives: { tooltip: {} } },
  });
}

describe('TechniciansView write controls', () => {
  beforeEach(() => {
    apiGet.mockReset().mockResolvedValue(TECHS);
    hasPermission.mockReset();
  });

  it('offers create, edit and delete to a user holding users.write', async () => {
    hasPermission.mockImplementation((k) => k === 'users.write');
    const w = mountView();
    await flushPromises();
    expect(w.find('[data-testid="new-tech-btn"]').exists()).toBe(true);
    expect(w.find('[aria-label="Edit"]').exists()).toBe(true);
    expect(w.find('[aria-label="Delete"]').exists()).toBe(true);
  });

  it('shows the list read-only without users.write', async () => {
    hasPermission.mockReturnValue(false);
    const w = mountView();
    await flushPromises();
    expect(w.text()).toContain('Terry Tech');
    expect(w.find('[data-testid="new-tech-btn"]').exists()).toBe(false);
    expect(w.find('[aria-label="Edit"]').exists()).toBe(false);
    expect(w.find('[aria-label="Delete"]').exists()).toBe(false);
    await w.find('tbody tr').trigger('click');
    await flushPromises();
    expect(w.vm.showDialog).toBe(false);
  });
});
