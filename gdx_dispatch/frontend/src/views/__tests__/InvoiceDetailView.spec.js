/**
 * InvoiceDetailView — Bill-To card (2026-05-21).
 *
 * Pins:
 *  1. /api/invoices/{id} customer_email/phone/address render on the Bill-To card.
 *  2. Missing email surfaces a "+ Add email" affordance (anchor with the
 *     bill-to-add-email testid) — keeps the Email-invoice flow unblocked.
 *  3. Clicking "Edit Customer" calls GET /api/customers/{id} (warms the dialog
 *     with the full customer record so notes/access_notes survive a save).
 *  4. Tel/mailto/maps links are wired when fields are present.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
const toastAdd = vi.fn();
const routerPush = vi.fn();
const confirmRequire = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  openAuthedFile: vi.fn(),
  createAuthedBlobUrl: vi.fn(),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));
vi.mock('primevue/useconfirm', () => ({
  useConfirm: () => ({ require: confirmRequire }),
}));
const routerReplace = vi.fn();
const routeMock = { params: { id: 'inv-1' }, query: {} };

vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: routerPush, replace: routerReplace }),
}));

import InvoiceDetailView from '../InvoiceDetailView.vue';

const baseStubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'outlined', 'rounded', 'disabled', 'size', 'loading', 'type'],
    emits: ['click'],
    template: '<button :type="type || \'button\'" :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  Dialog: {
    props: ['visible', 'header'],
    emits: ['update:visible'],
    template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>',
    inheritAttrs: false,
  },
  DataTable: {
    props: ['value'],
    template: '<div><slot name="empty" v-if="!value?.length" /><slot /></div>',
  },
  Column: { template: '<span><slot /></span>' },
  Tag: {
    props: ['value', 'severity'],
    template: '<span :data-testid="$attrs[\'data-testid\']">{{ value }}</span>',
    inheritAttrs: false,
  },
  Divider: { template: '<hr />' },
  Select: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<select :data-testid="$attrs[\'data-testid\']" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"></select>',
    inheritAttrs: false,
  },
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
  },
  Toast: { template: '<div />' },
  LineItemEditor: { template: '<div />' },
  CustomerFormDialog: {
    props: ['visible', 'mode', 'customer'],
    emits: ['update:visible', 'saved'],
    template: '<div v-if="visible" data-testid="customer-form-dialog">stub-dialog:{{ customer?.id }}:{{ customer?.email }}</div>',
  },
};

function buildInvoicePayload(overrides = {}) {
  return {
    id: 'inv-1',
    invoice_number: 'INV-0001',
    customer_id: 'cust-1',
    customer_name: 'Acme Door Co',
    customer_email: 'ops@acme.example',
    customer_phone: '555-0142',
    customer_address: '123 Main St',
    status: 'draft',
    effective_status: 'draft',
    subtotal: 75,
    tax_rate: 0.07,
    tax_amount: 5.25,
    total: 80.25,
    balance_due: 80.25,
    invoice_date: '2026-05-21',
    due_date: '2026-06-20',
    created_at: '2026-05-21T12:00:00Z',
    notes: '',
    lines: [],
    payments: [],
    ...overrides,
  };
}

function mountView() {
  return mount(InvoiceDetailView, { global: { stubs: baseStubs } });
}

function buildComposePayload(overrides = {}) {
  return {
    to: ['ops@acme.example'],
    subject: 'Invoice INV-0001 from Acme',
    body_text: 'Hi,\n\nYour invoice is attached.',
    pdf: {
      name: 'INV-0001.pdf',
      content_type: 'application/pdf',
      content_base64: btoa('%PDF-1.4 test-bytes'),
      size_bytes: 18,
    },
    extra_attachments: [],
    ...overrides,
  };
}

function mockApi(invoicePayload, customerPayload = null) {
  apiGet.mockImplementation((url) => {
    if (url === '/api/invoices/inv-1') return Promise.resolve(invoicePayload);
    if (url === '/api/invoices/inv-1/email-compose') return Promise.resolve(buildComposePayload());
    if (url === '/api/customers/cust-1') return Promise.resolve(customerPayload || { id: 'cust-1', name: 'Acme Door Co' });
    if (url === '/api/tax/config') return Promise.resolve({ default_rate: 0.07 });
    if (url === '/api/qb/dashboard') return Promise.resolve({ connected: false });
    if (url === '/api/qb/status') return Promise.resolve({ connected: false });
    return Promise.resolve({});
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  apiDel.mockReset();
  toastAdd.mockReset();
  routerPush.mockReset();
  routerReplace.mockReset();
  confirmRequire.mockReset();
  routeMock.query = {};
});

describe('InvoiceDetailView — Bill-To card', () => {
  it('renders the customer name/email/phone/address from the invoice payload', async () => {
    mockApi(buildInvoicePayload());
    const wrapper = mountView();
    await flushPromises();

    const email = wrapper.get('[data-testid="bill-to-email"] a');
    expect(email.text()).toBe('ops@acme.example');
    expect(email.attributes('href')).toBe('mailto:ops@acme.example');

    const phone = wrapper.get('[data-testid="bill-to-phone"] a');
    expect(phone.text()).toBe('555-0142');
    expect(phone.attributes('href')).toBe('tel:555-0142');

    const addr = wrapper.get('[data-testid="bill-to-address"] a');
    expect(addr.text()).toBe('123 Main St');
    expect(addr.attributes('href')).toContain('maps.google.com');

    expect(wrapper.get('[data-testid="bill-to-name"]').text()).toContain('Acme Door Co');
  });

  it('surfaces a "+ Add email" affordance when email is missing', async () => {
    mockApi(buildInvoicePayload({ customer_email: '' }));
    const wrapper = mountView();
    await flushPromises();

    const addEmail = wrapper.get('[data-testid="bill-to-add-email"]');
    expect(addEmail.text()).toMatch(/add email/i);
  });

  it('GETs /api/customers/{id} and opens the dialog when Edit Customer is clicked', async () => {
    mockApi(buildInvoicePayload(), {
      id: 'cust-1', name: 'Acme Door Co', email: 'ops@acme.example',
      phone: '555-0142', address: '123 Main St',
      notes: 'VIP', access_notes: 'Use back gate',
    });
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-customer-btn"]').trigger('click');
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/customers/cust-1');
    const dialog = wrapper.find('[data-testid="customer-form-dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain('cust-1');
  });
});

describe('InvoiceDetailView — edit save tax rate', () => {
  it('PATCHes an EXPLICIT tax_rate of 0 when the rate is zeroed (null would preserve the old tax dollars)', async () => {
    mockApi(buildInvoicePayload());
    apiPatch.mockResolvedValue({});
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-tax-rate"]').setValue('0');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/invoices/inv-1',
      expect.objectContaining({ tax_rate: 0 }),
    );
  });
});

describe('InvoiceDetailView — send composer PDF preview (2026-07-20)', () => {
  // jsdom has no URL.createObjectURL — install a stub so the preview iframe
  // branch renders, and restore whatever was there after each test.
  const ORIG_CREATE = URL.createObjectURL;
  const ORIG_REVOKE = URL.revokeObjectURL;

  afterEach(() => {
    URL.createObjectURL = ORIG_CREATE;
    URL.revokeObjectURL = ORIG_REVOKE;
  });

  it('Send Invoice opens the composer with an inline preview of the attached PDF', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:mock-pdf');
    URL.revokeObjectURL = vi.fn();
    mockApi(buildInvoicePayload());
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="send-invoice-btn"]').trigger('click');
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/invoices/inv-1/email-compose');
    expect(wrapper.find('[data-testid="invoice-composer"]').exists()).toBe(true);
    const frame = wrapper.get('[data-testid="composer-pdf-frame"]');
    expect(frame.attributes('src')).toContain('blob:mock-pdf');
    // Nothing is sent by opening the dialog — the POST only fires on the
    // explicit Send click.
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('degrades to a "still attached" note when inline preview is unavailable', async () => {
    URL.createObjectURL = undefined;
    mockApi(buildInvoicePayload());
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="send-invoice-btn"]').trigger('click');
    await flushPromises();

    const preview = wrapper.get('[data-testid="composer-pdf-preview"]');
    expect(preview.text()).toMatch(/still attached/i);
    expect(wrapper.find('[data-testid="composer-pdf-frame"]').exists()).toBe(false);
  });

  it('already-sent invoices get an enabled "Re-send Invoice" button (first send may have gone out PDF-less)', async () => {
    mockApi(buildInvoicePayload({ status: 'sent', effective_status: 'sent' }));
    const wrapper = mountView();
    await flushPromises();

    const btn = wrapper.get('[data-testid="send-invoice-btn"]');
    expect(btn.text()).toBe('Re-send Invoice');
    expect(btn.attributes('disabled')).toBeUndefined();
  });

  it('paid invoices keep the send button disabled', async () => {
    mockApi(buildInvoicePayload({ status: 'paid', effective_status: 'paid' }));
    const wrapper = mountView();
    await flushPromises();

    const btn = wrapper.get('[data-testid="send-invoice-btn"]');
    expect(btn.attributes('disabled')).toBeDefined();
  });

  it('?compose=1 auto-opens the composer (Billing list Send lands here) and strips the flag', async () => {
    routeMock.query = { compose: '1' };
    mockApi(buildInvoicePayload());
    const wrapper = mountView();
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/invoices/inv-1/email-compose');
    expect(routerReplace).toHaveBeenCalledWith({ query: {} });
    expect(wrapper.find('[data-testid="invoice-composer"]').exists()).toBe(true);
  });

  it('?compose=1 on a paid invoice does NOT auto-open the composer (mirrors the disabled button)', async () => {
    routeMock.query = { compose: '1' };
    mockApi(buildInvoicePayload({ status: 'paid', effective_status: 'paid' }));
    const wrapper = mountView();
    await flushPromises();

    expect(apiGet).not.toHaveBeenCalledWith('/api/invoices/inv-1/email-compose');
    expect(wrapper.find('[data-testid="invoice-composer"]').exists()).toBe(false);
  });
});

describe('InvoiceDetailView — composer body escapes then linkifies URLs (2026-07-21)', () => {
  // Source pin: the "Pay online: https://…/pay/{token}" line the compose
  // endpoint now prefills must arrive clickable — Outlook desktop does not
  // auto-link plain text inside HTML bodies. Escaping must happen BEFORE
  // linkifying or the injected <a> tags would themselves get escaped.
  const { readFileSync } = require('node:fs');
  const { join } = require('node:path');
  const SRC = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

  it('sendComposer escapes the body, then wraps bare URLs in anchors', () => {
    const start = SRC.indexOf('async function sendComposer');
    expect(start).toBeGreaterThan(-1);
    const body = SRC.slice(start, SRC.indexOf('/api/outlook/send', start));
    const escapeAt = body.indexOf('&amp;');
    const linkifyAt = body.indexOf('<a href=');
    expect(escapeAt).toBeGreaterThan(-1);
    expect(linkifyAt).toBeGreaterThan(escapeAt);
    expect(body).toContain('https?:\\/\\/'); // the URL-matching regex is present
  });
});
