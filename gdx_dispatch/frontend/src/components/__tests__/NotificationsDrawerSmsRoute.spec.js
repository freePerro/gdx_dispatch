/**
 * A scheduled text that did not go must be a link to where it shows.
 *
 * modules/phone_com/scheduled.py writes `category: "sms"` bell rows when a
 * scheduled reply, invoice text or estimate text is skipped, fails or goes
 * unconfirmed. The SMS thread is where the office sees what happened and
 * resends it; a fall-through to `default` would close the drawer and land
 * nowhere.
 *
 * MOUNTS and clicks, like the payment sibling. A source-text assertion would
 * only prove someone typed the word "sms".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';

const push = vi.fn();
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(async () => true) }),
}));

const markRead = vi.fn();
const store = {
  items: [],
  loading: false,
  markRead,
  remove: vi.fn(),
  clearAll: vi.fn(),
  fetchList: vi.fn(),
};
vi.mock('../../stores/notifications', () => ({
  useNotificationsStore: () => store,
}));

import NotificationsDrawer from '../NotificationsDrawer.vue';

const stubs = {
  Drawer: { props: ['visible'], template: '<div><slot name="header" /><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
  ProgressSpinner: { template: '<div />' },
};

function mountDrawer() {
  return mount(NotificationsDrawer, { props: { modelValue: true }, global: { stubs } });
}

const NOT_SENT = {
  id: 's1',
  title: 'Scheduled text not sent',
  message: 'The scheduled invoice text to Pat Payer: Nothing is owed on this invoice.',
  category: 'sms',
  is_read: false,
  created_at: '2026-10-01T13:01:00+00:00',
};

beforeEach(() => {
  push.mockClear();
  markRead.mockClear();
  store.items = [];
  store.loading = false;
});

describe('NotificationsDrawer — scheduled text notifications', () => {
  it('routes the office to the SMS page', async () => {
    window.history.replaceState({}, '', '/dashboard');
    store.items = [NOT_SENT];
    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');
    expect(push).toHaveBeenCalledWith('/phone-com/messages');
    expect(markRead).toHaveBeenCalledWith('s1');
  });

  it('routes a phone to the mobile SMS screen', async () => {
    window.history.replaceState({}, '', '/mobile/jobs');
    store.items = [{ ...NOT_SENT, id: 's2' }];
    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');
    expect(push).toHaveBeenCalledWith('/mobile/sms');
  });
});
