/**
 * TechniciansView — a technician links to a real login (GDXA-1583).
 *
 * The New Technician form used to send `user_id: email || name slug`, a value
 * no per-tech join (assignments, locations, day close) can match. The dialog
 * now picks the login from the user list and sends that user's id; with no
 * login picked there is no Save.
 *
 * jsdom applies no media queries — this proves wiring, never layout.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission: (k) => k === 'users.write',
    permissions: { value: [] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));

import TechniciansView from '../TechniciansView.vue';

const TERRY = '11111111-1111-4111-8111-111111111111';
const PAT = '22222222-2222-4222-8222-222222222222';
const USERS = [
  { id: TERRY, name: 'Terry Tech', email: 'terry@example.com', phone: '' },
  { id: PAT, name: 'Pat New', email: 'pat@example.com', phone: '555-0100' },
];
let techs;

function mountView() {
  return mount(TechniciansView, {
    global: { plugins: [PrimeVue], directives: { tooltip: {} } },
  });
}

describe('TechniciansView login picker', () => {
  beforeEach(() => {
    techs = [{ id: 't1', user_id: TERRY, name: 'Terry Tech', skills: [], hourly_rate: 40, active: true }];
    apiGet.mockReset().mockImplementation(async (url) => (url.startsWith('/api/users') ? USERS : techs));
    apiPost.mockReset().mockResolvedValue({ id: 't2' });
    apiPatch.mockReset().mockResolvedValue({});
  });

  it('creates with the picked user id, never an email or name slug', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-testid="new-tech-btn"]').trigger('click');
    await flushPromises();
    w.vm.form.name = 'Pat New';
    w.vm.form.email = 'pat@example.com';
    await flushPromises();
    // No login picked: Save is off and nothing is posted.
    expect(w.vm.canSave).toBe(false);
    await w.vm.saveTech();
    expect(apiPost).not.toHaveBeenCalled();

    w.vm.form.user_id = PAT;
    w.vm.fillFromUser();
    await flushPromises();
    expect(w.vm.canSave).toBe(true);
    await w.vm.saveTech();
    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/technicians');
    expect(body.user_id).toBe(PAT);
    expect(body.phone).toBe('555-0100'); // filled from the picked login
  });

  it('leaves out a login that already has a technician row', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-testid="new-tech-btn"]').trigger('click');
    await flushPromises();
    expect(w.vm.userOptions.map((o) => o.value)).toEqual([PAT]);
  });

  it('flags an old slug row, still saves it as it is, and relinks once a login is picked', async () => {
    techs = [{ id: 't9', user_id: 'walk683.inactive.tech', name: 'Walk', skills: [], hourly_rate: 0, active: true }];
    const w = mountView();
    await flushPromises();
    await w.find('[aria-label="Edit"]').trigger('click');
    await flushPromises();
    expect(w.vm.unlinked).toBe(true);
    expect(document.body.innerHTML).toContain('not linked to a login');

    // Deactivating a departed tech must not wait on a login: the stored link
    // is left out of the PATCH, so the server does not re-check it.
    w.vm.form.active = false;
    expect(w.vm.canSave).toBe(true);
    await w.vm.saveTech();
    expect(apiPatch.mock.calls[0][1]).not.toHaveProperty('user_id');
    expect(apiPatch.mock.calls[0][1].active).toBe(false);

    await w.find('[aria-label="Edit"]').trigger('click');
    await flushPromises();
    w.vm.form.user_id = 'not-a-listed-login';
    await flushPromises();
    expect(w.vm.canSave).toBe(false);
    w.vm.form.user_id = TERRY;
    await flushPromises();
    expect(w.vm.unlinked).toBe(false);
    await w.vm.saveTech();
    expect(apiPatch).toHaveBeenLastCalledWith('/api/technicians/t9', expect.objectContaining({ user_id: TERRY }));
  });

  it('treats an undashed stored id as the same login', async () => {
    techs = [{ id: 't1', user_id: TERRY.replace(/-/g, ''), name: 'Terry Tech', skills: [], hourly_rate: 40, active: true }];
    const w = mountView();
    await flushPromises();
    await w.find('[data-testid="new-tech-btn"]').trigger('click');
    await flushPromises();
    expect(w.vm.userOptions.map((o) => o.value)).toEqual([PAT]);
  });
});
