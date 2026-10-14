/**
 * "Clear all" says what it will do to whom.
 *
 * Office alerts are shared rows: an office user's Clear all clears them for
 * the whole office. A technician never sees those rows (the server filters
 * them out without nav.office), so their Clear all only touches their own —
 * telling them it "removes them for everyone" would be false.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
const confirmAsync = vi.fn(async () => true);
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync }),
}));
let isOffice = true;
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({ hasPermission: (key) => key === 'nav.office' && isOffice }),
}));

const clearAll = vi.fn();
const store = {
  items: [{ id: 'n1', title: 'T', message: 'M', category: 'system', is_read: false, created_at: '' }],
  loading: false,
  markRead: vi.fn(),
  remove: vi.fn(),
  clearAll,
  fetchList: vi.fn(),
};
vi.mock('../../stores/notifications', () => ({
  useNotificationsStore: () => store,
}));

import NotificationsDrawer from '../NotificationsDrawer.vue';

const stubs = {
  Drawer: { props: ['visible'], template: '<div><slot name="header" /><slot /></div>' },
  Button: { template: '<button v-bind="$attrs"><slot /></button>' },
  ProgressSpinner: true,
};

function clickClearAll() {
  const wrapper = mount(NotificationsDrawer, { props: { modelValue: true }, global: { stubs } });
  return wrapper.find('[data-testid="notif-clear-all"]').trigger('click');
}

describe('NotificationsDrawer — Clear all wording', () => {
  beforeEach(() => {
    confirmAsync.mockClear();
    clearAll.mockClear();
  });

  it('warns an office user that office alerts clear for everyone', async () => {
    isOffice = true;
    await clickClearAll();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(confirmAsync.mock.calls[0][0].message).toMatch(/everyone in the office/);
    expect(clearAll).toHaveBeenCalledTimes(1);
  });

  it('tells a technician it clears only their own', async () => {
    isOffice = false;
    await clickClearAll();
    expect(confirmAsync.mock.calls[0][0].message).toBe('Clear all your notifications?');
    expect(confirmAsync.mock.calls[0][0].message).not.toMatch(/everyone/);
    expect(clearAll).toHaveBeenCalledTimes(1);
  });
});
