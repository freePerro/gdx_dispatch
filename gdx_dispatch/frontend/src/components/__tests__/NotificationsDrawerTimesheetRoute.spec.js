/**
 * A held timesheet must be a link to the screen that fixes it.
 *
 * `tasks/payroll_timesheet` writes `category: "timesheet"` rows — one when
 * the period sends, and one when it is HELD because a shift is still open or
 * shows impossible hours. The held one is the whole point of the alert: it
 * names the shifts and the correction is made on /timesheets. Without a case
 * block the click falls through to `default`, which closes the drawer and
 * navigates nowhere — the office reads "Timesheet held", taps it, and lands
 * back where they started with nothing to act on.
 *
 * MOUNTS and clicks, like the payment sibling. A source-text assertion would
 * only prove someone typed the word "timesheet".
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

const HELD = {
  id: 'n1',
  title: 'Timesheet held — payroll not sent',
  message: '2026-08-10 – 2026-08-23 was not sent — Michael Tallman 2026-08-18 '
    + '(clocked out, no duration recorded). Correct them on Timesheets and it sends itself.',
  category: 'timesheet',
  is_read: false,
  created_at: '2026-08-24T12:00:00+00:00',
};

beforeEach(() => {
  push.mockClear();
  markRead.mockClear();
  store.items = [];
  store.loading = false;
});

describe('NotificationsDrawer — timesheet notifications', () => {
  it('routes a held timesheet to the screen that corrects it', async () => {
    window.history.replaceState({}, '', '/dashboard');
    store.items = [HELD];

    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');

    expect(push).toHaveBeenCalledWith('/timesheets');
    expect(markRead).toHaveBeenCalledWith('n1');
  });

  it('goes to the desk screen even from a phone', async () => {
    // There is no mobile timesheets route to send to, and correcting another
    // person's shift is office work. Landing nowhere would be worse.
    window.history.replaceState({}, '', '/mobile/jobs');
    store.items = [{ ...HELD, id: 'n2', is_read: true }];

    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');

    expect(push).toHaveBeenCalledWith('/timesheets');
    expect(markRead).not.toHaveBeenCalled();
  });

  it('the sent confirmation is a link too', async () => {
    window.history.replaceState({}, '', '/dashboard');
    store.items = [{
      id: 'n3',
      title: 'Timesheet sent to payroll',
      message: '9.00 hours for 1 person, 2026-08-10 – 2026-08-23 — sent to bookkeeper@example.com.',
      category: 'timesheet',
      is_read: true,
      created_at: '2026-08-24T12:00:00+00:00',
    }];

    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');

    expect(push).toHaveBeenCalledWith('/timesheets');
  });
});
