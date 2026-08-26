/**
 * A Stripe payment notification must be a link, not a shrug.
 *
 * `core/office_notifications.notify_payment_received` writes rows with
 * `category: "payment"`. Before this case existed the drawer's
 * `_destinationFor` fell through to `default`, which closes the drawer and
 * navigates nowhere — the office sees "Jane Doe paid $500 on INV-000123",
 * taps it, and lands back where they started.
 *
 * This MOUNTS the drawer and clicks the row, so it fails if the case block is
 * removed AND if the click handler stops routing. A source-text assertion
 * would only have proved someone typed the word "payment".
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
  // Drawer teleports and gates on `visible`; render the slot inline so the
  // list is in the wrapper and clickable.
  Drawer: { props: ['visible'], template: '<div><slot name="header" /><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
  ProgressSpinner: { template: '<div />' },
};

function mountDrawer() {
  return mount(NotificationsDrawer, {
    props: { modelValue: true },
    global: { stubs },
  });
}

beforeEach(() => {
  push.mockClear();
  markRead.mockClear();
  store.items = [];
  store.loading = false;
});

describe('NotificationsDrawer — payment notifications', () => {
  it('routes a payment notification to the invoices screen on desktop', async () => {
    window.history.replaceState({}, '', '/dashboard');
    store.items = [{
      id: 'n1',
      title: 'Payment received',
      message: 'Jane Doe paid $500.00 on INV-000123 by card',
      category: 'payment',
      is_read: false,
      created_at: '2026-08-26T12:00:00+00:00',
    }];

    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');

    expect(push).toHaveBeenCalledWith('/invoices');
    expect(markRead).toHaveBeenCalledWith('n1');
  });

  it('routes a payment notification to mobile billing from a /mobile route', async () => {
    window.history.replaceState({}, '', '/mobile/jobs');
    store.items = [{
      id: 'n2',
      title: 'Payment received',
      message: 'Jane Doe paid $500.00 on INV-000123 by bank transfer',
      category: 'payment',
      is_read: true,
      created_at: '2026-08-26T12:00:00+00:00',
    }];

    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');

    expect(push).toHaveBeenCalledWith('/mobile/billing');
    // Already read — the drawer must not re-POST /read.
    expect(markRead).not.toHaveBeenCalled();
  });

  it('counterfactual: an unmapped category still navigates nowhere', async () => {
    window.history.replaceState({}, '', '/dashboard');
    store.items = [{
      id: 'n3',
      title: 'Something',
      message: 'no destination for this',
      category: 'not_a_real_category',
      is_read: true,
      created_at: '2026-08-26T12:00:00+00:00',
    }];

    const wrapper = mountDrawer();
    await wrapper.find('.notif-item').trigger('click');

    expect(push).not.toHaveBeenCalled();
  });
});
