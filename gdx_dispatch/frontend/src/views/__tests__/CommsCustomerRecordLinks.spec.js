/**
 * The class guard for the COMMS surfaces: a call, an email or an SMS thread
 * names its customer, and that name is the route to the customer record.
 *
 * Sibling of MobileCustomerRecordLinks.spec.js, which pins the same class on
 * the job / estimate / invoice surfaces (#777). Six surfaces live here:
 *
 *   MobilePhoneView      call detail sheet        -> /mobile/customers/:id
 *   MobileInboxView      message detail chip      -> /mobile/customers/:id
 *   MobileSmsView        conversation header      -> /mobile/customers/:id
 *   InboxView            detail-pane chip         -> /customers/:id
 *   PhoneComCallsView    call detail dialog       -> /customers/:id
 *   PhoneComMessagesView conversation pane title  -> /customers/:id
 *
 * The two inbox chips are the sharp case: both were a <span> wearing the
 * primary colour and a matching border, so they already READ as links and did
 * nothing. A control that looks clickable and isn't costs the reader a click
 * to discover, which is worse than plain text.
 *
 * Pinned per surface:
 *  1. id + name present -> a router-link to the record.
 *  2. id missing        -> plain text, never an anchor.
 *  3. name missing      -> plain text. _link_labels degrades to
 *                          "linked, unnamed" when the label lookup fails, and
 *                          an anchor whose only content is the word 'Customer'
 *                          has no accessible name.
 *  4. soft-deleted      -> the name STAYS READABLE and only the link is
 *                          withheld. GET /api/customers/{id} 404s on a deleted
 *                          record, so linking it is a dead end; blanking the
 *                          name instead would lose who the message was from
 *                          AND keep the dead link. The payloads carry
 *                          customer_deleted / linked_customer_deleted for
 *                          exactly this.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: apiGet,
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    patch: vi.fn(),
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn().mockResolvedValue(true) }),
}));

const stubs = {
  RouterLink: RouterLinkStub,
  AppLayout: { template: '<div><slot /></div>' },
  // The header slot is the whole point on MobileSmsView — a Dialog stub that
  // drops it would make that surface untestable here.
  Dialog: {
    props: ['visible'],
    template:
      "<div v-if='visible'><slot name='header' /><slot /><slot name='footer' /></div>",
  },
  Card: {
    template:
      "<div><slot name='title' /><slot name='content' /><slot /></div>",
  },
  Toolbar: { template: "<div><slot name='start' /><slot name='end' /></div>" },
  Button: {
    props: ['label', 'icon', 'loading', 'severity', 'text', 'size', 'disabled', 'outlined', 'rounded'],
    emits: ['click'],
    template: '<button v-bind="$attrs" @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Tag: { props: ['value', 'severity'], template: '<span>{{ value }}</span>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  Select: { props: ['modelValue', 'options'], template: '<div />' },
  DatePicker: { props: ['modelValue'], template: '<div />' },
  ProgressSpinner: { template: '<div />' },
  Paginator: { template: '<div />' },
  EmailBodyFrame: { template: '<div />' },
  EmailAttachments: { template: '<div />' },
  Column: { template: '<div />' },
  // The real DataTable's row-click is what opens the call dialog. The stub
  // reproduces just that: one clickable element per row, emitting the same
  // { data } shape the component's handler destructures.
  DataTable: {
    props: ['value'],
    emits: ['row-click'],
    template:
      '<div><button class="dt-row" v-for="(r, i) in (value || [])" :key="i"' +
      ' @click="$emit(\'row-click\', { data: r })">row</button></div>',
  },
};

beforeEach(() => {
  setActivePinia(createPinia());
  apiGet.mockReset();
  if (!globalThis.matchMedia) {
    globalThis.matchMedia = () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    });
  }
});

afterEach(() => {
  vi.restoreAllMocks();
});

const LIVE = { customer_id: 'c1', customer_name: 'Acme Doors', customer_deleted: false };

// RouterLinkStub renders an <a> with no href — the destination lives on the
// `to` prop — so every assertion about where a link GOES has to read the prop
// off the specific stub, not an attribute, and not merely the first stub on a
// page that has several.
function linkTo(wrapper, testId) {
  const link = wrapper
    .findAllComponents(RouterLinkStub)
    .find((c) => c.attributes('data-test') === testId);
  expect(link, `no RouterLink carrying data-test="${testId}"`).toBeTruthy();
  return link.props('to');
}

// ── MobilePhoneView: the call detail sheet ────────────────────────────────
// GET /api/phone-com/calls/{id} (get_call_detail) carries customer_id,
// customer_name and customer_deleted.
describe('MobilePhoneView — the call detail names the customer and routes to it', () => {
  async function openCall(detail) {
    const { default: View } = await import('../MobilePhoneView.vue');
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/calls?')) {
        return { items: [{ id: 'call-1', direction: 'in', from_number: '+15551112222' }], total: 1 };
      }
      if (url === '/api/phone-com/calls/call-1') return { id: 'call-1', direction: 'in', ...detail };
      return {};
    });
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.find('[data-test="mp-call-row"]').trigger('click');
    await flushPromises();
    return w;
  }

  it('links the name to /mobile/customers/:id when id and name are present', async () => {
    const w = await openCall(LIVE);
    const link = w.find('[data-test="mp-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(linkTo(w, 'mp-customer-link')).toBe('/mobile/customers/c1');
  });

  it('renders plain text, not an anchor, when the customer id is missing', async () => {
    const w = await openCall({ customer_id: null, customer_name: 'Acme Doors' });
    expect(w.find('[data-test="mp-customer-link"]').exists()).toBe(false);
    expect(w.find('.detail-meta').text()).toContain('Acme Doors');
  });

  it('keeps the name readable and withholds only the link when soft-deleted', async () => {
    const w = await openCall({ ...LIVE, customer_deleted: true });
    expect(w.find('[data-test="mp-customer-link"]').exists()).toBe(false);
    expect(w.find('.detail-meta').text()).toContain('Acme Doors');
  });
});

// ── MobileInboxView: the message detail chip ──────────────────────────────
describe('MobileInboxView — the detail chip routes to the customer record', () => {
  const MSG = {
    id: 'm1',
    subject: 'Broken spring',
    from_address: 'alice@example.com',
    received_at: '2026-09-20T12:00:00Z',
    is_read: true,
    has_attachments: false,
  };

  async function openMessage(detailLinks) {
    const { default: View } = await import('../MobileInboxView.vue');
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/outlook/messages?')) return { items: [MSG], has_more: false };
      if (url === '/api/outlook/messages/m1') return { ...MSG, ...detailLinks };
      if (url.includes('/body')) return { fetched: true, content_type: 'html', body_html: '<p>hi</p>' };
      if (url.includes('/thread')) return [];
      return {};
    });
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
    return w;
  }

  it('links the chip to /mobile/customers/:id when id and name are present', async () => {
    const w = await openMessage({ linked_customer_id: 'c1', linked_customer_name: 'Acme Doors' });
    const link = w.find('[data-test="mi-detail-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(linkTo(w, 'mi-detail-customer-link')).toBe('/mobile/customers/c1');
  });

  it('leaves the unnamed chip inert rather than making an anchor out of the word Customer', async () => {
    const w = await openMessage({ linked_customer_id: 'c1', linked_customer_name: null });
    expect(w.find('[data-test="mi-detail-customer-link"]').exists()).toBe(false);
    expect(w.find('[data-test="mi-detail-links"]').text()).toContain('Customer');
  });

  it('keeps the name readable and withholds only the link when soft-deleted', async () => {
    const w = await openMessage({
      linked_customer_id: 'c1',
      linked_customer_name: 'Acme Doors',
      linked_customer_deleted: true,
    });
    expect(w.find('[data-test="mi-detail-customer-link"]').exists()).toBe(false);
    expect(w.find('[data-test="mi-detail-links"]').text()).toContain('Acme Doors');
  });
});

// ── MobileSmsView: the conversation header ────────────────────────────────
// The header was the Dialog's :header PROP, which can only be a string — the
// link needs the #header slot. The close icon lives in the dialog's own
// header-icons block and is unaffected.
describe('MobileSmsView — the conversation header routes to the customer record', () => {
  async function openThread(thread) {
    const { default: View } = await import('../MobileSmsView.vue');
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/messages/threads?')) {
        return { items: [{ thread_key: 't1', other_party_number: '+15551112222', ...thread }] };
      }
      if (url.includes('/api/phone-com/messages/threads/t1')) return { items: [] };
      return {};
    });
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.find('[data-test="ms-thread-row"]').trigger('click');
    await flushPromises();
    return w;
  }

  it('links the name to /mobile/customers/:id when id and name are present', async () => {
    const w = await openThread(LIVE);
    const link = w.find('[data-test="ms-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(linkTo(w, 'ms-customer-link')).toBe('/mobile/customers/c1');
  });

  it('falls back to the phone number, unlinked, for a thread with no customer', async () => {
    const w = await openThread({ customer_id: null, customer_name: null });
    expect(w.find('[data-test="ms-customer-link"]').exists()).toBe(false);
    expect(w.find('.thread-title').text()).toBe('+15551112222');
  });

  it('keeps the name readable and withholds only the link when soft-deleted', async () => {
    const w = await openThread({ ...LIVE, customer_deleted: true });
    expect(w.find('[data-test="ms-customer-link"]').exists()).toBe(false);
    expect(w.find('.thread-title').text()).toBe('Acme Doors');
  });

  // Taking the #header slot removes the <span id> PrimeVue derives the dialog's
  // accessible name from, so the modal announces with NO name unless the view
  // supplies the pairing itself. A stubbed Dialog cannot reproduce PrimeVue's
  // internals, so this pins the two halves that have to agree; the real
  // behaviour was checked by mounting the unstubbed Dialog by hand.
  it('names the dialog for a screen reader in both the linked and unlinked states', async () => {
    for (const thread of [LIVE, { customer_id: null, customer_name: null }]) {
      const w = await openThread(thread);
      const dialog = w.findComponent(stubs.Dialog);
      expect(dialog.attributes('aria-labelledby')).toBe('ms-thread-title');
      const titled = w.find('#ms-thread-title');
      expect(titled.exists()).toBe(true);
      expect(titled.text().trim().length).toBeGreaterThan(0);
    }
  });
});

// ── InboxView (desktop): the detail-pane chip ─────────────────────────────
describe('InboxView — the detail chip routes to the customer record', () => {
  const MSG = {
    id: 'm1',
    subject: 'Broken spring',
    from_address: 'alice@example.com',
    to_addresses: ['office@example.com'],
    direction: 'inbound',
    received_at: '2026-09-20T12:00:00Z',
    is_read: true,
    has_attachments: false,
    conversation_id: 'conv-1',
  };

  // InboxView reads through useApi (fetchFolders -> api.get), so it is driven
  // by the same apiGet mock as the rest of this file. InboxViewP1P2.spec.js
  // stubs globalThis.fetch instead — that works only because it leaves useApi
  // real, and mocking both would silently bypass the fetch layer.
  async function openMessage(detailLinks) {
    const { default: View } = await import('../InboxView.vue');
    apiGet.mockImplementation(async (url) => {
      if (url.endsWith('/api/outlook/folders')) {
        return [{
          id: 'f1', graph_folder_id: 'g-inbox', display_name: 'Inbox', parent_folder_id: null,
          well_known_name: 'inbox', total_count: 1, unread_count: 0, child_folder_count: 0,
          is_hidden: false, depth: 0, is_system: true, color: null, pinned: false, sort_order: 0,
        }];
      }
      if (url.includes('/api/outlook/messages?')) {
        return { items: [MSG], has_more: false, next_offset: 1 };
      }
      if (url.includes('/thread')) return [];
      if (url.includes('/body')) {
        return { fetched: true, content_type: 'html', body_html: '<p>hi</p>' };
      }
      if (url.includes('/api/outlook/messages/m1')) {
        return { ...MSG, ...detailLinks, viewer_is_owner: true };
      }
      return {};
    });
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.findAll('[data-test="inbox-row"]')[0].trigger('click');
    await flushPromises();
    return w;
  }

  it('links the chip to /customers/:id when id and name are present', async () => {
    const w = await openMessage({ linked_customer_id: 'c1', linked_customer_name: 'Acme Doors' });
    const link = w.find('[data-test="inbox-detail-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(linkTo(w, 'inbox-detail-customer-link')).toBe('/customers/c1');
  });

  it('leaves the unnamed chip inert rather than making an anchor out of the word Customer', async () => {
    const w = await openMessage({ linked_customer_id: 'c1', linked_customer_name: null });
    expect(w.find('[data-test="inbox-detail-customer-link"]').exists()).toBe(false);
    expect(w.find('[data-test="inbox-detail-links"]').text()).toContain('Customer');
  });

  it('keeps the name readable and withholds only the link when soft-deleted', async () => {
    const w = await openMessage({
      linked_customer_id: 'c1',
      linked_customer_name: 'Acme Doors',
      linked_customer_deleted: true,
    });
    expect(w.find('[data-test="inbox-detail-customer-link"]').exists()).toBe(false);
    expect(w.find('[data-test="inbox-detail-links"]').text()).toContain('Acme Doors');
  });
});

// ── PhoneComCallsView (desktop): the call detail dialog ───────────────────
describe('PhoneComCallsView — the call detail names the customer and routes to it', () => {
  async function openCall(detail) {
    const { default: View } = await import('../PhoneComCallsView.vue');
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/calls?')) {
        return { items: [{ id: 'call-1', direction: 'in', from_number: '+15551112222' }], total: 1 };
      }
      if (url === '/api/phone-com/calls/call-1') return { id: 'call-1', direction: 'in', ...detail };
      return { items: [], total: 0 };
    });
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.find('.dt-row').trigger('click');
    await flushPromises();
    return w;
  }

  it('links the name to /customers/:id when id and name are present', async () => {
    const w = await openCall(LIVE);
    const link = w.find('[data-test="pc-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(linkTo(w, 'pc-customer-link')).toBe('/customers/c1');
  });

  it('renders plain text, not an anchor, when the customer id is missing', async () => {
    const w = await openCall({ customer_id: null, customer_name: 'Acme Doors' });
    expect(w.find('[data-test="pc-customer-link"]').exists()).toBe(false);
    expect(w.text()).toContain('Acme Doors');
  });

  it('keeps the name readable and withholds only the link when soft-deleted', async () => {
    const w = await openCall({ ...LIVE, customer_deleted: true });
    expect(w.find('[data-test="pc-customer-link"]').exists()).toBe(false);
    expect(w.text()).toContain('Acme Doors');
  });
});

// ── PhoneComMessagesView (desktop): the conversation pane title ───────────
describe('PhoneComMessagesView — the conversation title routes to the record', () => {
  async function openThread(thread) {
    const { default: View } = await import('../PhoneComMessagesView.vue');
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/messages/threads?')) {
        return { items: [{ thread_key: 't1', other_party_number: '+15551112222', ...thread }] };
      }
      if (url.includes('/api/phone-com/messages/threads/t1')) return { items: [] };
      return {};
    });
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.find('[data-test="pc-thread-row"]').trigger('click');
    await flushPromises();
    return w;
  }

  it('links the name to /customers/:id when id and name are present', async () => {
    const w = await openThread(LIVE);
    const link = w.find('[data-test="pcm-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(linkTo(w, 'pcm-customer-link')).toBe('/customers/c1');
  });

  it('falls back to the phone number, unlinked, for a thread with no customer', async () => {
    const w = await openThread({ customer_id: null, customer_name: null });
    expect(w.find('[data-test="pcm-customer-link"]').exists()).toBe(false);
    expect(w.find('.pane-header').text()).toContain('+15551112222');
  });

  it('keeps the name readable and withholds only the link when soft-deleted', async () => {
    const w = await openThread({ ...LIVE, customer_deleted: true });
    expect(w.find('[data-test="pcm-customer-link"]').exists()).toBe(false);
    expect(w.find('.pane-header').text()).toContain('Acme Doors');
  });
});
