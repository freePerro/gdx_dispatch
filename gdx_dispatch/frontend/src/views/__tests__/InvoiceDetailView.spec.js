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
 *  5. GDXA-5 — the dialog NEVER opens without a full record. The dialog PATCHes
 *     its whole field set, so opening it on the invoice's four-field customer
 *     projection silently erases notes / referral_source / customer_type.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import { setActivePinia, createPinia } from 'pinia';
import { useAuthStore } from '../../stores/auth';

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
    // notes/customer_type are rendered because they are the fields the real
    // dialog PATCHes but the invoice payload never carries — GDXA-5 is
    // exactly "the dialog opened without them". Make them observable.
    template: '<div v-if="visible" data-testid="customer-form-dialog">stub-dialog:{{ customer?.id }}:{{ customer?.email }}:{{ customer?.notes }}:{{ customer?.customer_type }}</div>',
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
    // §11 rail (2026-08-08): Send/Mark-mailed on an UNVERIFIED draft now
    // detours through the verify-and-continue confirm. These composer tests
    // exercise the send mechanics, so the fixture ships verified; the rail
    // itself is pinned in InvoiceDeliveryRail.spec.js.
    verified_at: '2026-05-21T12:30:00Z',
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
  // The view resolves `invoices.write` through usePermission -> useAuthStore
  // to decide whether to offer Void (2026-08-23). Without an active Pinia the
  // store throws during setup and EVERY test in this file dies at mount.
  // A fresh pinia per test also means no permission leaks between them: the
  // store starts empty, so `hasPermission` is false and the Void button is
  // hidden unless a test seeds it.
  setActivePinia(createPinia());
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

    // The read is quiet: this function owns the failure path (it redirects),
    // so useApi's default error toast would be a second, wrong story.
    expect(apiGet).toHaveBeenCalledWith('/api/customers/cust-1', { suppressErrorToast: true });
    const dialog = wrapper.find('[data-testid="customer-form-dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain('cust-1');
  });
});

/**
 * GDXA-5 — opening the shared CustomerFormDialog on a partial record.
 *
 * CustomerFormDialog.submitForm PATCHes name/phone/email/address/notes/
 * referral_source/customer_type unconditionally, and `update_customer` uses
 * `exclude_unset` — which explicit nulls survive. So a dialog opened on the
 * invoice's four-field customer projection writes `notes: null`,
 * `referral_source: null`, `customer_type: "Residential"` over the record, on
 * a toast that reads "Customer Updated". The audit row holds only the new
 * values, so the erased text is not recoverable.
 *
 * The guard is therefore: open on a full record or do not open at all.
 */
describe('InvoiceDetailView — Edit Customer never opens on a partial record (GDXA-5)', () => {
  function mockApiWithCustomer(customerResponder) {
    apiGet.mockImplementation((url) => {
      if (url === '/api/invoices/inv-1') return Promise.resolve(buildInvoicePayload({ customer_email: '' }));
      if (url === '/api/customers/cust-1') return customerResponder();
      if (url === '/api/tax/config') return Promise.resolve({ default_rate: 0.07 });
      if (url === '/api/qb/dashboard') return Promise.resolve({ connected: false });
      if (url === '/api/qb/status') return Promise.resolve({ connected: false });
      return Promise.resolve({});
    });
  }

  it('routes to the customer record instead of opening the dialog when the GET fails', async () => {
    const boom = Object.assign(new Error('Something went wrong'), { status: 503 });
    mockApiWithCustomer(() => Promise.reject(boom));
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-customer-btn"]').trigger('click');
    await flushPromises();

    // The dialog is the thing that would have done the damage.
    expect(wrapper.find('[data-testid="customer-form-dialog"]').exists()).toBe(false);
    expect(routerPush).toHaveBeenCalledWith('/customers/cust-1');
    // Not a silent navigation — the user is told why they moved.
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn', summary: expect.stringMatching(/couldn't load the customer/i) }),
    );
  });

  it('routes away on a 200 with no record, not just on an error', async () => {
    // The pre-fix try-branch read `result?.data || result || {...projection}`,
    // so a 200 carrying an empty body reached the identical destructive state
    // with no error at all. `id` is the real-record test.
    mockApiWithCustomer(() => Promise.resolve({}));
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-customer-btn"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-testid="customer-form-dialog"]').exists()).toBe(false);
    expect(routerPush).toHaveBeenCalledWith('/customers/cust-1');
  });

  it('routes away from the "+ Add email" link too — same guard, same function', async () => {
    // The likeliest way in: the email is missing, which is exactly when the
    // office clicks this. It must not become a way to blank the record.
    mockApiWithCustomer(() => Promise.reject(new Error('Failed to fetch')));
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="bill-to-add-email"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-testid="customer-form-dialog"]').exists()).toBe(false);
    expect(routerPush).toHaveBeenCalledWith('/customers/cust-1');
  });

  it('opens on the full record, carrying the fields the invoice payload lacks', async () => {
    mockApiWithCustomer(() => Promise.resolve({
      id: 'cust-1', name: 'Acme Door Co', email: 'ops@acme.example',
      phone: '555-0142', address: '123 Main St',
      notes: 'Gate code 4417', referral_source: 'Chamber', customer_type: 'Commercial',
    }));
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-customer-btn"]').trigger('click');
    await flushPromises();

    const dialog = wrapper.get('[data-testid="customer-form-dialog"]');
    // These two are what a save would otherwise have erased.
    expect(dialog.text()).toContain('Gate code 4417');
    expect(dialog.text()).toContain('Commercial');
    expect(routerPush).not.toHaveBeenCalledWith('/customers/cust-1');
  });
});

describe('InvoiceDetailView — margin override persistence (2026-08-20 audit)', () => {
  // The editor AUTO-FILLS the Margin column with the tier-implied margin so the
  // operator can see what they're running at. That fill is not a decision.
  // saveEdit must store a margin only when a human actually set one — and must
  // NOT drop one that was already stored.
  //
  // LineItemEditor is stubbed in this spec, so these drive the exact line
  // shapes the real editor emits and assert the PATCH body — which is where
  // the defect lived, and where there was previously no coverage at all.
  const LINE_STUB = {
    props: ['lines'],
    emits: ['update:lines', 'update:fromPartIds'],
    template: `
      <div>
        <button data-testid="emit-autofilled" @click="$emit('update:lines', [{
          id: 'ln-1', description: 'Widget', quantity: 1, unit_price: 100,
          taxable: true, category: 'Parts', cost: 50, margin_pct_override: 50,
          _autoMargin: 50
        }])">autofilled</button>
        <button data-testid="emit-stored" @click="$emit('update:lines', [{
          id: 'ln-1', description: 'Widget', quantity: 1, unit_price: 150,
          taxable: true, category: 'Parts', cost: 87, margin_pct_override: 42,
          _marginPersisted: true, _marginUserEdited: true
        }])">stored</button>
      </div>`,
  };

  function mountWithLineStub() {
    return mount(InvoiceDetailView, {
      global: { stubs: { ...baseStubs, LineItemEditor: LINE_STUB } },
    });
  }

  it('does NOT store a tier-autofilled margin', async () => {
    mockApi(buildInvoicePayload());
    apiPatch.mockResolvedValue({});
    const wrapper = mountWithLineStub();
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-autofilled"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();

    const lineCall = apiPatch.mock.calls.find(([url]) => url.includes('/lines/'));
    // Assert unconditionally: `if (lineCall)` would let this pass by simply
    // never saving, which is the failure mode it is supposed to detect.
    expect(lineCall).toBeTruthy();
    expect(lineCall[1].margin_pct_override).toBeNull();
  });

  it('KEEPS a margin the operator actually set', async () => {
    // The mirror case, and the one two rounds of fixes kept breaking: gating
    // too tightly silently cleared a real stored override.
    mockApi(buildInvoicePayload());
    apiPatch.mockResolvedValue({});
    const wrapper = mountWithLineStub();
    await flushPromises();

    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-stored"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();

    const lineCall = apiPatch.mock.calls.find(([url]) => url.includes('/lines/'));
    expect(lineCall).toBeTruthy();
    expect(lineCall[1].margin_pct_override).toBeCloseTo(0.42, 4);
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

  it('paid invoices get an enabled "Send Receipt" button (2026-08-17 — the paid copy is the receipt)', async () => {
    mockApi(buildInvoicePayload({ status: 'paid', effective_status: 'paid' }));
    const wrapper = mountView();
    await flushPromises();

    const btn = wrapper.get('[data-testid="send-invoice-btn"]');
    expect(btn.text()).toBe('Send Receipt');
    expect(btn.attributes('disabled')).toBeUndefined();
  });

  it('void invoices keep the send button disabled', async () => {
    mockApi(buildInvoicePayload({ status: 'void', effective_status: 'void' }));
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

  it('?compose=1 on a paid invoice DOES auto-open the composer (Billing "Send Receipt" lands here)', async () => {
    routeMock.query = { compose: '1' };
    mockApi(buildInvoicePayload({ status: 'paid', effective_status: 'paid' }));
    const wrapper = mountView();
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/invoices/inv-1/email-compose');
    expect(wrapper.find('[data-testid="invoice-composer"]').exists()).toBe(true);
  });

  it('?compose=1 on a void invoice does NOT auto-open the composer (mirrors the disabled button)', async () => {
    routeMock.query = { compose: '1' };
    mockApi(buildInvoicePayload({ status: 'void', effective_status: 'void' }));
    const wrapper = mountView();
    await flushPromises();

    expect(apiGet).not.toHaveBeenCalledWith('/api/invoices/inv-1/email-compose');
    expect(wrapper.find('[data-testid="invoice-composer"]').exists()).toBe(false);
  });
});

describe('InvoiceDetailView — composer sends plain text; the SERVER renders the email (2026-08-18)', () => {
  // Email overhaul: escaping + linkifying moved server-side (core/email_layout,
  // pytest-covered) so composer sends and one-click sends produce the same
  // branded email. The browser must no longer hand-roll email HTML.
  const { readFileSync } = require('node:fs');
  const { join } = require('node:path');
  const SRC = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

  it('sendComposer posts body_text to the invoice send endpoint, builds no HTML', () => {
    const start = SRC.indexOf('async function sendComposer');
    expect(start).toBeGreaterThan(-1);
    const end = SRC.indexOf('async function _emailViaMailtoFallback', start);
    const body = SRC.slice(start, end);
    expect(body).toMatch(/api\.post\(`\/api\/invoices\/\$\{route\.params\.id\}\/send`/);
    expect(body).toContain('body_text:');
    expect(body).toContain('contact_id:');
    // The legacy client-built email is gone: no <pre> wrapper, no direct
    // Outlook relay from this path.
    expect(body).not.toContain('<pre');
    expect(body).not.toContain('/api/outlook/send');
  });
});

/**
 * The job link on the normalized invoice (2026-08-12).
 *
 * Found by driving a real browser, not by a test: `normalizeInvoice` copies
 * fields explicitly and `job_id` was never among them, so `invoice.job_id` was
 * permanently undefined. Every consequence followed from that one omission —
 * the job-photo picker's `v-if` could not be true, and `fetchJobPhotos()`
 * returned early without ever calling the server. The feature shipped in
 * v1.44.0 and had rendered for nobody since; production has zero invoices
 * carrying a photo, which is the same fact seen from the database end.
 */
describe('InvoiceDetailView — the invoice keeps its job link', () => {
  it('normalizes job_id, so job-scoped features can render', async () => {
    mockApi({
      id: 'inv-1',
      invoice_number: 'INV-0001',
      job_id: 'job-42',
      customer_id: 'cust-1',
      status: 'draft',
      total: 100,
      lines: [],
      attached_photo_ids: [],
    });
    const w = mountView();
    await flushPromises();

    // The photo picker is the visible consequence; it renders for any
    // job-linked invoice now, with an honest empty state when the job has no
    // photos.
    expect(w.find('[data-testid="invoice-job-photos"]').exists()).toBe(true);
    expect(w.find('[data-testid="invoice-photos-empty"]').exists()).toBe(true);
  });

  it('fetches the job photos once the invoice is loaded', async () => {
    mockApi({
      id: 'inv-1',
      invoice_number: 'INV-0001',
      job_id: 'job-42',
      customer_id: 'cust-1',
      status: 'draft',
      total: 100,
      lines: [],
      attached_photo_ids: [],
    });
    mountView();
    await flushPromises();

    // Before the fix this call never happened: job_id was undefined and
    // fetchJobPhotos() bailed on its own guard.
    expect(apiGet.mock.calls.map(([u]) => u)).toContain('/api/jobs/job-42/photos');
  });

  it('offers no photo card on a counter-sale invoice (no job)', async () => {
    mockApi({
      id: 'inv-1',
      invoice_number: 'INV-0001',
      job_id: null,
      customer_id: 'cust-1',
      status: 'draft',
      total: 100,
      lines: [],
    });
    const w = mountView();
    await flushPromises();
    expect(w.find('[data-testid="invoice-job-photos"]').exists()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// includes_labor round-trip (2026-08-19).
//
// This is where billing actually happens: a closeout autodraft lands here and
// the office opens Edit. An adversarial review caught the checkbox rendering,
// accepting a tick, showing a success toast, and silently discarding it --
// the normalizer dropped the field, the dirty-check ignored it, and the PATCH
// body omitted it. Same class of bug S122-b already fixed once for
// category/cost/margin. These pin the whole chain.
// ---------------------------------------------------------------------------
describe('InvoiceDetailView — includes_labor survives a save', () => {
  const { readFileSync } = require('node:fs');
  const { join } = require('node:path');
  const SRC = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

  it('the normalizer carries includes_labor off the wire', () => {
    const i = SRC.indexOf('const lineItems = (payload.lines');
    expect(i).toBeGreaterThan(-1);
    expect(SRC.slice(i, i + 1400)).toMatch(/includes_labor:\s*Boolean\(item\.includes_labor\)/);
  });

  it('edit mode snapshots it, so a ticked line renders ticked', () => {
    const i = SRC.indexOf('editLines.value = invoice.value.line_items.map');
    expect(i).toBeGreaterThan(-1);
    expect(SRC.slice(i, i + 900)).toMatch(/includes_labor:\s*Boolean\(ln\.includes_labor\)/);
  });

  it('the dirty-check notices it — ticking ONLY the box must still save', () => {
    const i = SRC.indexOf('const changed =');
    expect(i).toBeGreaterThan(-1);
    expect(SRC.slice(i, i + 700)).toMatch(
      /Boolean\(orig\.includes_labor\)\s*!==\s*Boolean\(ln\.includes_labor\)/,
    );
  });

  it('the PATCH body carries it, so the endpoint has a real caller', () => {
    const i = SRC.indexOf('const patch = {');
    expect(i).toBeGreaterThan(-1);
    expect(SRC.slice(i, i + 900)).toMatch(/patch\.includes_labor\s*=/);
  });

  it('a brand-new line POSTs it too', () => {
    const i = SRC.indexOf('const body = {');
    expect(i).toBeGreaterThan(-1);
    expect(SRC.slice(i, i + 700)).toMatch(/body\.includes_labor\s*=\s*true/);
  });
});

// ---------------------------------------------------------------------------
// Unbilled-parts banner (2026-08-19).
//
// job-closeout-billing-visibility-plan §8 decided in 2026-07: build the
// invoice from everything priced, leave the rest on the checklist, and MARK
// THE INVOICE. Only the mobile lane ever got the mark, so the office verified
// labor-only drafts with nothing saying attested parts had been dropped --
// the rubber-stamp failure that plan predicted at its own line 913.
// ---------------------------------------------------------------------------
describe('InvoiceDetailView — unbilled parts banner', () => {
  const { readFileSync } = require('node:fs');
  const { join } = require('node:path');
  const SRC = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

  it('asks the unbilled-parts endpoint on load', () => {
    const i = SRC.indexOf('async function fetchUnbilledJobParts');
    expect(i).toBeGreaterThan(-1);
    const span = SRC.slice(i, i + 900);
    expect(span).toMatch(/parts-needed\?status=ordered,received,used&unbilled=true/);
    expect(span).toMatch(/suppressErrorToast/);
  });

  it('only fires for a DRAFT invoice that has a job', () => {
    const i = SRC.indexOf('async function fetchUnbilledJobParts');
    const span = SRC.slice(i, i + 700);
    expect(span).toMatch(/invoice\.value\?\.job_id/);
    expect(span).toMatch(/=== "draft"/);
    expect(span).toMatch(/if \(!jobId \|\| !isDraft\) return;/);
  });

  it('a failed read never blocks the page', () => {
    const i = SRC.indexOf('async function fetchUnbilledJobParts');
    const span = SRC.slice(i, i + 2200);
    expect(span).toMatch(/catch \(e\) \{[\s\S]{0,120}unbilledJobParts\.value = \[\];/);
  });

  it('renders the banner with a count and a way to act on it', () => {
    const i = SRC.indexOf('data-testid="unbilled-parts-banner"');
    expect(i).toBeGreaterThan(-1);
    const block = SRC.slice(i, i + 1200);
    expect(SRC).toMatch(/v-else-if="unbilledJobParts\.length"/);
    expect(block).toMatch(/data-testid="unbilled-parts-edit"/);
    expect(block).toMatch(/enterEditMode/);
  });

  it('verify posts straight through — no confirm dialog stands between', async () => {
    // Was a source-text test asserting the handler's comment mentioned #215.
    // It asserted authorship, not behaviour, and its premise expired: #215 is
    // FIXED — useDestructiveConfirm now resolves useConfirm() during setup()
    // and AppLayout mounts <ConfirmDialog/>, so confirms really do confirm.
    // What still matters is that Verify is not behind one, because the real
    // second gate is the SERVER's unbilled-parts refusal. Asserted by
    // clicking it.
    const auth = useAuthStore();
    auth.user = { id: 'u1', email: 'office@example.com', role: 'accounting' };
    auth.accessToken = 'test-token';
    auth.permissions = new Set(['invoices.write']);
    auth.permissionsLoaded = true;
    mockApi(buildInvoicePayload({ verified_at: null }));
    const wrapper = mountView();
    await flushPromises();
    await nextTick();

    apiPost.mockResolvedValueOnce({ verified_at: '2026-08-23T12:00:00Z' });
    await wrapper.find('[data-testid="verify-invoice-btn"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/invoices/inv-1/verify', {}, expect.anything(),
    );
  });

  it('banner styling uses theme tokens, not hardcoded light colours', () => {
    const style = SRC.slice(SRC.indexOf('.unbilled-parts-banner'), SRC.indexOf('.unbilled-parts-banner') + 500);
    expect(style).toMatch(/var\(--/);
    expect(style).not.toMatch(/background:\s*#fff/i);
  });

  // Audit round 2: 403 is not "nothing missing". The accounting role has
  // invoices.write but NOT inventory.read, so the user who verifies drafts
  // gets a permission error here -- and an empty banner reads as an
  // all-clear on a money screen.
  it('distinguishes no-permission from nothing-missing', () => {
    const i = SRC.indexOf('async function fetchUnbilledJobParts');
    const span = SRC.slice(i, i + 1800);
    expect(span).toMatch(/e\?\.status === 403/);
    expect(span).toMatch(/unbilledPartsError\.value = "forbidden"/);
    expect(SRC).toMatch(/data-testid="unbilled-parts-forbidden"/);
  });

  // Audit round 2: "unbilled" is job-wide; the banner claims something
  // narrower. Reporting a part that IS already on this invoice sends the
  // office to add a second line for a part already charged.
  it('excludes parts already lined on THIS invoice', () => {
    const i = SRC.indexOf('async function fetchUnbilledJobParts');
    const span = SRC.slice(i, i + 1800);
    expect(span).toMatch(/linedPartIds/);
    expect(span).toMatch(/line_items \|\| \[\]/);
    expect(span).toMatch(/!linedPartIds\.has\(String\(p\.id\)\)/);
  });
});

// ── Void (2026-08-23) ───────────────────────────────────────────────────────
// `POST /api/invoices/{id}/void` had existed since GL S5 with ZERO UI callers,
// so the office could not void an invoice at all. These assert BEHAVIOUR --
// what renders -- not that the source contains a string.
describe('InvoiceDetailView — Void', () => {
  async function mountWith({ permissions = [], role = 'accounting', status = 'draft' } = {}) {
    const auth = useAuthStore();
    // `isAuthenticated` derives from accessToken, and `isAdmin` from the role
    // claim — an admin/owner short-circuits hasPermission entirely, so the
    // "hidden" case has to be seeded as a non-admin role or it proves nothing.
    auth.user = { id: 'u1', email: 'office@example.com', role };
    auth.accessToken = 'test-token';
    auth.permissions = new Set(permissions);
    auth.permissionsLoaded = true;
    // normalizeInvoice reads `effective_status` FIRST, so setting `status`
    // alone leaves the view on the payload's default and the assertion tests
    // nothing. Set both.
    mockApi(buildInvoicePayload({ status, effective_status: status }));
    const wrapper = mountView();
    await flushPromises();
    await nextTick();
    return wrapper;
  }

  it('offers Void to a role that holds invoices.write', async () => {
    const wrapper = await mountWith({ permissions: ['invoices.write'] });
    expect(wrapper.find('[data-testid="void-invoice-btn"]').exists()).toBe(true);
  });

  it('hides Void from a role that does not', async () => {
    // The technician tier. The API also 403s them, but a button that only
    // ever errors is a dead end -- do not draw it.
    const wrapper = await mountWith({ permissions: ['jobs.write', 'inventory.write'], role: 'technician' });
    expect(wrapper.find('[data-testid="void-invoice-btn"]').exists()).toBe(false);
  });

  it('does not offer Void on an already-void invoice, and tags it instead', async () => {
    const wrapper = await mountWith({ permissions: ['invoices.write'], status: 'void' });
    expect(wrapper.find('[data-testid="void-invoice-btn"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="invoice-void-tag"]').exists()).toBe(true);
  });

  it('will not fire until the invoice number is retyped exactly', async () => {
    const wrapper = await mountWith({ permissions: ['invoices.write'] });
    await wrapper.find('[data-testid="void-invoice-btn"]').trigger('click');
    await nextTick();

    const confirmBtn = () => wrapper.find('[data-testid="void-confirm-btn"]');
    expect(confirmBtn().attributes('disabled')).toBeDefined();

    const input = wrapper.find('[data-testid="void-confirm-input"]');
    await input.setValue('INV-0002');
    await nextTick();
    expect(confirmBtn().attributes('disabled'),
      'a different invoice number must not unlock the void').toBeDefined();

    await input.setValue('INV-0001');
    await nextTick();
    expect(confirmBtn().attributes('disabled'),
      'the exact number must unlock it').toBeUndefined();

    expect(apiPost).not.toHaveBeenCalledWith(
      expect.stringContaining('/void'), expect.anything(), expect.anything(),
    );
  });

  it('explains the payments blocker instead of letting the operator earn a 409', async () => {
    // The server refuses a void while any non-voided payment exists. An
    // earlier draft only *claimed* to guard this in a comment: the button
    // rendered, the operator retyped the whole invoice number, then ate the
    // 409. Adversarial review caught the gap between comment and v-if.
    const auth = useAuthStore();
    auth.user = { id: 'u1', email: 'office@example.com', role: 'accounting' };
    auth.accessToken = 'test-token';
    auth.permissions = new Set(['invoices.write']);
    auth.permissionsLoaded = true;
    mockApi(buildInvoicePayload({
      status: 'paid', effective_status: 'paid', amount_paid: 80.25,
      payments: [{ id: 'p1', amount: 80.25, method: 'card', date: '2026-05-22' }],
    }));
    const wrapper = mountView();
    await flushPromises();
    await nextTick();

    // Still offered — hiding it would leave someone wanting to void a mispaid
    // invoice with no control and no reason.
    await wrapper.find('[data-testid="void-invoice-btn"]').trigger('click');
    await nextTick();

    const blocked = wrapper.find('[data-testid="void-blocked-reason"]');
    expect(blocked.exists(), 'the dialog must say WHY it cannot proceed').toBe(true);
    expect(blocked.text()).toMatch(/payments/i);
    // No point offering the retype when it cannot unlock anything.
    expect(wrapper.find('[data-testid="void-confirm-input"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="void-confirm-btn"]').attributes('disabled')).toBeDefined();
  });

  it('re-fetches through normalizeInvoice instead of assigning the raw response', async () => {
    // The API speaks `lines`; this view speaks `line_items`. Assigning the
    // void response directly left line_items undefined and the totals computed
    // threw "Cannot read properties of undefined (reading 'reduce')" -- AFTER
    // the void had already succeeded server-side. Caught in a real browser.
    const wrapper = await mountWith({ permissions: ['invoices.write'] });
    apiPost.mockResolvedValueOnce({ id: 'inv-1', status: 'void', lines: [], payments: [] });

    await wrapper.find('[data-testid="void-invoice-btn"]').trigger('click');
    await nextTick();
    await wrapper.find('[data-testid="void-confirm-input"]').setValue('INV-0001');
    await nextTick();

    apiGet.mockClear();
    await wrapper.find('[data-testid="void-confirm-btn"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith('/api/invoices/inv-1/void', {}, expect.anything());
    expect(apiGet, 'a successful void must re-read the invoice, not trust the response shape')
      .toHaveBeenCalledWith('/api/invoices/inv-1');
  });
});

// ── Verify: the server's unbilled-parts gate (follow-up 2, 2026-08-23) ──────
// The banner was client-side, so it could not help the accounting role (holds
// invoices.write, NOT inventory.read — its fetch 403s), the mobile lane, or
// any API caller. The server now refuses; this screen must turn that refusal
// into a CHOICE, not a red toast the office cannot get past.
describe('InvoiceDetailView — verify unbilled-parts gate', () => {
  async function mounted() {
    const auth = useAuthStore();
    auth.user = { id: 'u1', email: 'office@example.com', role: 'accounting' };
    auth.accessToken = 'test-token';
    auth.permissions = new Set(['invoices.write']);
    auth.permissionsLoaded = true;
    mockApi(buildInvoicePayload({ verified_at: null }));
    const wrapper = mountView();
    await flushPromises();
    await nextTick();
    return wrapper;
  }

  function refusal() {
    // Shaped the way `useApi` actually throws: the parsed JSON body lands on
    // `err.body`. Writing this as `err.data` (what an earlier draft guessed)
    // made this test agree with the bug — the dialog never opened in a real
    // browser. Mirror the transport, do not invent it.
    return Object.assign(new Error('conflict'), {
      status: 409,
      body: {
        detail: {
          message: '1 recorded part from this job is not on this invoice.',
          unbilled_parts: [
            { id: 'p1', part_name: 'Torsion spring', quantity: 2, unit_price: 149 },
          ],
          acknowledge_field: 'acknowledge_unbilled_parts',
        },
      },
    });
  }

  it('shows what is missing instead of a dead-end error', async () => {
    const wrapper = await mounted();
    apiPost.mockRejectedValueOnce(refusal());

    await wrapper.find('[data-testid="verify-invoice-btn"]').trigger('click');
    await flushPromises();
    await nextTick();

    expect(wrapper.find('[data-testid="verify-unbilled-dialog"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="verify-unbilled-list"]').text()).toContain('Torsion spring');
    // A refusal is not a failure to report as one.
    expect(toastAdd).not.toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });

  it('verifies anyway with the acknowledgement the server asked for', async () => {
    const wrapper = await mounted();
    apiPost.mockRejectedValueOnce(refusal());
    await wrapper.find('[data-testid="verify-invoice-btn"]').trigger('click');
    await flushPromises();
    await nextTick();

    apiPost.mockResolvedValueOnce({ verified_at: '2026-08-23T12:00:00Z' });
    await wrapper.find('[data-testid="verify-anyway-btn"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenLastCalledWith(
      '/api/invoices/inv-1/verify',
      { acknowledge_unbilled_parts: true },
      expect.anything(),
    );
  });

  it('does not acknowledge on the FIRST attempt', async () => {
    // The whole gate is defeated if the client always sends the override.
    const wrapper = await mounted();
    apiPost.mockResolvedValueOnce({ verified_at: '2026-08-23T12:00:00Z' });

    await wrapper.find('[data-testid="verify-invoice-btn"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/invoices/inv-1/verify', {}, expect.anything(),
    );
  });

  it('does not acknowledge when the button hands it a click EVENT', async () => {
    // The bug a real browser caught and this file's stub hid. `@click="fn"`
    // passes Vue's MouseEvent as the first argument; the handler's first
    // parameter is `acknowledgeUnbilledParts`, and a MouseEvent is truthy —
    // so every click silently acknowledged a warning nobody was shown.
    //
    // The shared Button stub emits `click` with NO payload, so the standard
    // trigger('click') above cannot see it. This one emits an event the way
    // the real PrimeVue button does.
    const auth = useAuthStore();
    auth.user = { id: 'u1', email: 'office@example.com', role: 'accounting' };
    auth.accessToken = 'test-token';
    auth.permissions = new Set(['invoices.write']);
    auth.permissionsLoaded = true;
    mockApi(buildInvoicePayload({ verified_at: null }));

    const EVENT_EMITTING_BUTTON = {
      props: ['label', 'icon', 'severity', 'outlined', 'disabled', 'loading', 'text', 'rounded', 'size', 'type'],
      emits: ['click'],
      template:
        '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\', $event)">{{ label }}</button>',
      inheritAttrs: false,
    };
    const wrapper = mount(InvoiceDetailView, {
      global: { stubs: { ...baseStubs, Button: EVENT_EMITTING_BUTTON } },
    });
    await flushPromises();
    await nextTick();

    apiPost.mockResolvedValueOnce({ verified_at: '2026-08-23T12:00:00Z' });
    await wrapper.find('[data-testid="verify-invoice-btn"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/invoices/inv-1/verify', {}, expect.anything(),
    );
  });

  it('Send also surfaces the gate instead of a bare "Verify failed"', async () => {
    // The SECOND verify caller on this screen. `ensureVerifiedForDelivery`
    // verifies inline when the office presses Send or Mark as Mailed on an
    // unverified draft — found by sweeping for callers rather than assuming
    // the Verify button was the only one. Without this it would report a bare
    // "Verify failed" with no list and no way forward.
    const wrapper = await mounted();
    // `ensureVerifiedForDelivery` asks "verify and continue?" first. The
    // shared confirm mock records the call and never answers it, so accept it
    // here or the flow parks on an unresolved promise and this test would
    // "fail" for a reason that has nothing to do with the gate.
    confirmRequire.mockImplementation((opts) => opts.accept && opts.accept());
    apiPost.mockRejectedValueOnce(refusal());

    await wrapper.find('[data-testid="mark-mailed-btn"]').trigger('click');
    await flushPromises();
    await nextTick();

    expect(wrapper.find('[data-testid="verify-unbilled-dialog"]').exists()).toBe(true);
    expect(toastAdd).not.toHaveBeenCalledWith(
      expect.objectContaining({ summary: 'Verify failed' }),
    );
    // Delivery must NOT proceed while the question stands.
    expect(apiPost).not.toHaveBeenCalledWith(
      expect.stringContaining('mark-sent'), expect.anything(), expect.anything(),
    );
  });

  it('still reports a real failure as an error', async () => {
    const wrapper = await mounted();
    apiPost.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }));

    await wrapper.find('[data-testid="verify-invoice-btn"]').trigger('click');
    await flushPromises();
    await nextTick();

    expect(wrapper.find('[data-testid="verify-unbilled-dialog"]').exists()).toBe(false);
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });
});

describe('InvoiceDetailView — M31+M33 edit-save guards', () => {
  const QTY_STUB = {
    props: ['lines'],
    emits: ['update:lines', 'update:fromPartIds'],
    template: `
      <div>
        <button data-testid="emit-cleared-qty" @click="$emit('update:lines', [{
          id: 'ln-1', description: 'Widget', quantity: null, unit_price: 100, taxable: true
        }])">cleared</button>
        <button data-testid="emit-loaded-negative" @click="$emit('update:lines', [{
          id: 'ln-1', description: 'QB discount', quantity: 1, unit_price: -100, taxable: true
        }])">loadedneg</button>
        <button data-testid="emit-new-negative" @click="$emit('update:lines', [{
          description: 'typo line', quantity: 1, unit_price: -25, taxable: true
        }])">newneg</button>
      </div>`,
  };
  function mountQty() {
    return mount(InvoiceDetailView, { global: { stubs: { ...baseStubs, LineItemEditor: QTY_STUB } } });
  }

  it('M31: edit save refuses a cleared quantity — never PATCHed as 1', async () => {
    mockApi(buildInvoicePayload());
    apiPatch.mockResolvedValue({});
    const wrapper = mountQty();
    await flushPromises();
    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-cleared-qty"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();
    const lineCall = apiPatch.mock.calls.find(([url]) => url.includes('/lines/'));
    expect(lineCall).toBeFalsy();
    // Audit: the first refusal wrote to an undefined ref (editSaving), threw,
    // and the outer catch added a bogus "Save failed" toast on top. The
    // refusal must be the ONLY reaction.
    const errToast = toastAdd.mock.calls.find(([t]) => t.severity === 'error');
    expect(errToast).toBeFalsy();
    const warn = toastAdd.mock.calls.find(([t]) => t.severity === 'warn');
    expect(warn).toBeTruthy();
  });

  it('M33: a LOADED negative line passes through unclamped', async () => {
    // The old unconditional Math.max(0, price) zeroed a loaded -$100 promo
    // line and the change-diff PATCHed the $0 in on any unrelated save,
    // silently raising the balance.
    mockApi(buildInvoicePayload());
    apiPatch.mockResolvedValue({});
    const wrapper = mountQty();
    await flushPromises();
    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-loaded-negative"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();
    const lineCall = apiPatch.mock.calls.find(([url]) => url.includes('/lines/'));
    expect(lineCall).toBeTruthy();
    // Audit: the server's schema is ge=0, so SENDING -100 422s the save. The
    // pass-through is an OMISSION — the server keeps its stored value.
    expect('unit_price' in lineCall[1]).toBe(false);
  });

  it('M33: a NEW negative line still clamps to 0 (typo protection for fresh rows)', async () => {
    mockApi(buildInvoicePayload());
    apiPost.mockResolvedValue({});
    apiPatch.mockResolvedValue({});
    const wrapper = mountQty();
    await flushPromises();
    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-new-negative"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();
    const newLine = apiPost.mock.calls.find(([url]) => url.includes('/lines'));
    expect(newLine).toBeTruthy();
    expect(newLine[1].unit_price).toBe(0);
  });
});


describe('InvoiceDetailView — GDXA-91: the edit save refuses BEFORE it writes', () => {
  // The class: a save that refuses mid-write. saveEdit is a multi-request diff
  // loop — per-line PATCH/POST, then per-line DELETE, then the invoice-level
  // PATCH carrying tax rate / dates / notes / hide_line_prices LAST. The M31
  // quantity refusal used to live INSIDE that loop, so a refusal the client
  // could have computed up front landed after earlier lines were committed.
  //
  // Why the pre-existing M31 pin could not catch it: it emits a single bad
  // line, so the loop refuses on iteration 1 and nothing has been written yet
  // — it passes before AND after the fix. The input that can turn red is a
  // GOOD line ordered BEFORE the bad one. Verified red against the pre-fix
  // view (test 1 failed on the /lines/ PATCH and the invoice PATCH).
  const GOOD_ID = '1111111111111111111111111111aaaa'; // >= 32 chars => server id
  const BAD_ID = '2222222222222222222222222222bbbb';

  function storedLines() {
    return [
      { id: GOOD_ID, description: 'Spring', quantity: 1, unit_price: 100, taxable: true, line_total: 100 },
      { id: BAD_ID, description: 'Warranty swap', quantity: 2, unit_price: 50, taxable: true, line_total: 100 },
    ];
  }

  // Emits the operator's edit: line A legitimately changed (qty 1 -> 3), line
  // B's quantity cleared. Order matters — A is first, so the pre-fix loop
  // PATCHed A and only then refused on B.
  const ORDER_STUB = {
    props: ['lines'],
    emits: ['update:lines', 'update:fromPartIds'],
    template: `
      <div>
        <button data-testid="emit-good-then-bad" @click="$emit('update:lines', [
          { id: '${GOOD_ID}', description: 'Spring', quantity: 3, unit_price: 100, taxable: true },
          { id: '${BAD_ID}', description: 'Warranty swap', quantity: null, unit_price: 50, taxable: true },
        ])">goodthenbad</button>
        <button data-testid="emit-two-bad" @click="$emit('update:lines', [
          { id: '${GOOD_ID}', description: 'Spring', quantity: 0, unit_price: 100, taxable: true },
          { id: '${BAD_ID}', description: 'Warranty swap', quantity: null, unit_price: 50, taxable: true },
        ])">twobad</button>
      </div>`,
  };

  function mountOrdered(overrides = {}) {
    mockApi(buildInvoicePayload({ lines: storedLines(), ...overrides }));
    apiPatch.mockResolvedValue({});
    apiPost.mockResolvedValue({});
    apiDel.mockResolvedValue({});
    return mount(InvoiceDetailView, {
      global: { stubs: { ...baseStubs, LineItemEditor: ORDER_STUB } },
    });
  }

  const lineWrites = () => [
    ...apiPatch.mock.calls.filter(([url]) => url.includes('/lines/')),
    ...apiPost.mock.calls.filter(([url]) => url.includes('/lines')),
    ...apiDel.mock.calls.filter(([url]) => url.includes('/lines/')),
  ];
  const invoiceWrites = () =>
    apiPatch.mock.calls.filter(([url]) => url === '/api/invoices/inv-1');

  it('refuses with ZERO writes when a good line is ordered before the bad one', async () => {
    const wrapper = mountOrdered();
    await flushPromises();
    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-good-then-bad"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();

    // The whole point: line A's qty 1 -> 3 must NOT have been committed.
    expect(lineWrites()).toEqual([]);
    // And the operator's invoice-level edits (tax rate, dates, notes,
    // hide_line_prices) must not have been silently dropped on the floor —
    // pre-fix this PATCH never fired at all because it is sequenced LAST.
    expect(invoiceWrites()).toEqual([]);

    const warn = toastAdd.mock.calls.filter(([t]) => t.severity === 'warn');
    expect(warn).toHaveLength(1);
    expect(warn[0][0].detail).toContain('Warranty swap');
    // The refusal is the ONLY reaction — no bogus "Save failed" on top.
    expect(toastAdd.mock.calls.find(([t]) => t.severity === 'error')).toBeFalsy();
    expect(toastAdd.mock.calls.find(([t]) => t.severity === 'success')).toBeFalsy();
  });

  it('the snapshot the view keeps rendering is still accurate after a refusal', async () => {
    // The refusal `return`s from inside the `try` WITHOUT throwing, so the
    // catch's resync GET never runs — pre-fix that made it the one failure
    // that left the read-only table rendering the pre-save snapshot over
    // half-committed rows, and Cancel then discarded an edit already
    // partially applied.
    //
    // A unit test cannot see the server, so the invariant is the PAIR:
    // zero writes AND no resync. Asserting "no resync" alone would pass
    // pre-fix too (that silence IS the defect) — it only means the view is
    // still accurate when nothing moved underneath it. Both assertions
    // together are what can turn red.
    const wrapper = mountOrdered();
    await flushPromises();
    const fetches = () =>
      apiGet.mock.calls.filter(([url]) => url === '/api/invoices/inv-1').length;
    expect(fetches()).toBe(1);

    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-good-then-bad"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();
    expect(lineWrites()).toEqual([]); // nothing moved...
    expect(invoiceWrites()).toEqual([]);
    expect(fetches()).toBe(1); // ...so no resync was owed, and none fired.

    // Edit mode stays open so the operator keeps their draft and can fix it,
    // and Cancel now discards an edit that really was never applied.
    expect(wrapper.find('[data-testid="invoice-edit-save"]').exists()).toBe(true);
    await wrapper.get('[data-testid="invoice-edit-cancel"]').trigger('click');
    await flushPromises();
    expect(lineWrites()).toEqual([]);
    expect(fetches()).toBe(1);
  });

  it('names EVERY offending line in one toast, not just the first', async () => {
    // Sibling parity (InvoiceCreateView / EstimateView / ChangeOrdersView):
    // a loop-bound refusal can only ever name the line it tripped on, so the
    // operator fixes one, re-saves, and gets told about the next.
    const wrapper = mountOrdered();
    await flushPromises();
    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit-two-bad"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();

    expect(lineWrites()).toEqual([]);
    expect(invoiceWrites()).toEqual([]);
    const warn = toastAdd.mock.calls.filter(([t]) => t.severity === 'warn');
    expect(warn).toHaveLength(1);
    expect(warn[0][0].detail).toContain('Spring');
    expect(warn[0][0].detail).toContain('Warranty swap');
  });

  it('a STORED quantity of 0 the operator never touched still refuses', async () => {
    // Reachability is wider than "the operator cleared a field":
    // normalizeInvoice keeps a stored 0 (`toNum(item.quantity ?? 1)`) and
    // enterEditMode maps it through recordedQuantity, which since #560 keeps
    // 0 as 0. So an invoice carrying a zero-quantity line cannot be saved at
    // all — an operator editing ONLY the tax rate or notes is refused.
    //
    // Honest labelling: this one passes BEFORE the fix as well — with a
    // single offending line the old in-loop refusal tripped on iteration 1,
    // before writing anything. It is a REACHABILITY pin, not the regression
    // net (that is the first test in this block).
    //
    // And the refusal is SETTLED, not an open question: the owner ruled on
    // 2026-09-11 (`core/quantities.py`, `zero_quantity_verdict`) that a
    // recorded 0 carrying money is refused rather than laundered into an
    // invoice, and that docstring already names this exact consequence —
    // "`InvoiceLinePatchIn` forbids quantity 0 so nothing can put it back …
    // the office cannot save the invoice at all". The same census recorded
    // 0 zero and 0 NULL quantities across 1,281 prod rows, so the block is
    // unreachable from real data; a contradictory row is fixed at its
    // source, where a person can see it.
    mockApi(buildInvoicePayload({
      lines: [{ id: GOOD_ID, description: 'Warranty swap', quantity: 0, unit_price: 0, taxable: true, line_total: 0 }],
    }));
    apiPatch.mockResolvedValue({});
    apiPost.mockResolvedValue({});
    apiDel.mockResolvedValue({});
    const wrapper = mount(InvoiceDetailView, {
      global: { stubs: { ...baseStubs, LineItemEditor: { template: '<div />' } } },
    });
    await flushPromises();
    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    // No line touched at all — straight to Save.
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();

    expect(lineWrites()).toEqual([]);
    expect(invoiceWrites()).toEqual([]);
    const warn = toastAdd.mock.calls.filter(([t]) => t.severity === 'warn');
    expect(warn).toHaveLength(1);
    expect(warn[0][0].detail).toContain('Warranty swap');
  });
});

describe('InvoiceDetailView — GDXA-91: the server bounds refuse before writing too', () => {
  // Same class, found by the pre-commit audit: InvoiceLineCreateIn /
  // InvoiceLinePatchIn (routers/invoices.py) bound description to 500 chars,
  // quantity to 9999 and unit_price/cost to 999999.99, while LineItemEditor
  // sets `:min` but no `:max` and no `maxlength`. Every one of those was a
  // refusal the client could compute up front that instead surfaced as a 422
  // raised from INSIDE the write loop — earlier lines already committed, and
  // the invoice-level PATCH (tax rate, dates, notes) dropped because it is
  // sequenced last. The operator saw "Save failed" and kept the wrong rate.
  const GOOD_ID = '1111111111111111111111111111aaaa';
  const BAD_ID = '2222222222222222222222222222bbbb';

  function stored(overrides = []) {
    return [
      { id: GOOD_ID, description: 'Spring', quantity: 1, unit_price: 100, taxable: true, line_total: 100 },
      { id: BAD_ID, description: 'Opener', quantity: 1, unit_price: 200, taxable: true, line_total: 200 },
      ...overrides,
    ];
  }

  function stubEmitting(lines) {
    return {
      props: ['lines'],
      emits: ['update:lines', 'update:fromPartIds'],
      // The view puts data-testid="invoice-edit-line-items" on this component,
      // and Vue merges a parent attr onto a SINGLE root element — which would
      // clobber our own testid. Wrap, so the button keeps its own.
      template: '<div><button data-testid="emit" @click="$emit(\'update:lines\', payload)">go</button></div>',
      data: () => ({ payload: lines }),
    };
  }

  async function attempt(storedLines, editedLines) {
    mockApi(buildInvoicePayload({ lines: storedLines }));
    apiPatch.mockResolvedValue({});
    apiPost.mockResolvedValue({});
    apiDel.mockResolvedValue({});
    const wrapper = mount(InvoiceDetailView, {
      global: { stubs: { ...baseStubs, LineItemEditor: stubEmitting(editedLines) } },
    });
    await flushPromises();
    await wrapper.get('[data-testid="invoice-edit-btn"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="emit"]').trigger('click');
    await wrapper.get('[data-testid="invoice-edit-save"]').trigger('click');
    await flushPromises();
    return {
      lineWrites: [
        ...apiPatch.mock.calls.filter(([u]) => u.includes('/lines/')),
        ...apiPost.mock.calls.filter(([u]) => u.includes('/lines')),
        ...apiDel.mock.calls.filter(([u]) => u.includes('/lines/')),
      ],
      invoiceWrites: apiPatch.mock.calls.filter(([u]) => u === '/api/invoices/inv-1'),
      warns: toastAdd.mock.calls.filter(([t]) => t.severity === 'warn'),
      errors: toastAdd.mock.calls.filter(([t]) => t.severity === 'error'),
    };
  }

  it('an over-long description refuses with zero writes (good line first)', async () => {
    const r = await attempt(stored(), [
      { id: GOOD_ID, description: 'Spring', quantity: 3, unit_price: 100, taxable: true },
      { id: BAD_ID, description: 'x'.repeat(501), quantity: 1, unit_price: 200, taxable: true },
    ]);
    expect(r.lineWrites).toEqual([]);
    expect(r.invoiceWrites).toEqual([]);
    expect(r.errors).toEqual([]);
    expect(r.warns).toHaveLength(1);
    expect(r.warns[0][0].detail).toContain('500 characters');
    // The toast names the line without dumping 501 characters into it.
    expect(r.warns[0][0].detail.length).toBeLessThan(200);
  });

  it('a quantity above 9999 refuses with zero writes (good line first)', async () => {
    const r = await attempt(stored(), [
      { id: GOOD_ID, description: 'Spring', quantity: 3, unit_price: 100, taxable: true },
      { id: BAD_ID, description: 'Opener', quantity: 10000, unit_price: 200, taxable: true },
    ]);
    expect(r.lineWrites).toEqual([]);
    expect(r.invoiceWrites).toEqual([]);
    expect(r.warns[0][0].detail).toContain('9999');
  });

  it('a unit price above 999999.99 refuses with zero writes (good line first)', async () => {
    const r = await attempt(stored(), [
      { id: GOOD_ID, description: 'Spring', quantity: 3, unit_price: 100, taxable: true },
      { id: BAD_ID, description: 'Opener', quantity: 1, unit_price: 1000000, taxable: true },
    ]);
    expect(r.lineWrites).toEqual([]);
    expect(r.invoiceWrites).toEqual([]);
    expect(r.warns[0][0].detail).toMatch(/Unit price above/);
  });

  it('a NEW line over the bounds refuses too (no id to diff against)', async () => {
    const r = await attempt(stored(), [
      { id: GOOD_ID, description: 'Spring', quantity: 3, unit_price: 100, taxable: true },
      { description: 'Fresh row', quantity: 20000, unit_price: 5, taxable: true },
    ]);
    expect(r.lineWrites).toEqual([]);
    expect(r.invoiceWrites).toEqual([]);
    expect(r.warns[0][0].detail).toContain('9999');
  });

  it('an UNTOUCHED stored value over the bounds still saves — the carve-out', async () => {
    // Deliberate and load-bearing. A stored out-of-bounds value cannot be
    // created through the API but is reachable by import, and today such an
    // invoice saves fine as long as nobody edits that line (the diff finds
    // no change, so no PATCH fires and nothing 422s). Bound-checking it in
    // the pre-pass would make a savable invoice unsavable — a behavior
    // change, not this fix. If this test ever goes red, someone widened the
    // pre-pass past "what the operator changed" and needs a ruling first.
    const importedLines = [
      { id: GOOD_ID, description: 'Spring', quantity: 1, unit_price: 100, taxable: true, line_total: 100 },
      { id: BAD_ID, description: 'y'.repeat(700), quantity: 1, unit_price: 200, taxable: true, line_total: 200 },
    ];
    const r = await attempt(importedLines, [
      // line 0 edited; line 1 passed through EXACTLY as stored.
      { id: GOOD_ID, description: 'Spring', quantity: 3, unit_price: 100, taxable: true },
      { id: BAD_ID, description: 'y'.repeat(700), quantity: 1, unit_price: 200, taxable: true },
    ]);
    expect(r.warns).toEqual([]);
    expect(r.lineWrites).toHaveLength(1); // only the line the operator changed
    expect(r.invoiceWrites).toHaveLength(1); // and the header edits landed
  });

  // Migration 108: an invoice line takes a two-decimal quantity. The save used
  // to `Math.floor` it, so 2.5 hours PATCHed as 2 (billing half an hour less
  // than typed) and 0.5 floored to 0 and was refused as "No quantity". The
  // typed value now goes to the server as it is, with no arithmetic — the
  // server 422s a third decimal place, so float noise must never be computed
  // into a quantity here.
  it('a fractional quantity is sent as typed, never floored', async () => {
    const r = await attempt(stored(), [
      { id: GOOD_ID, description: 'Spring', quantity: 2.5, unit_price: 100, taxable: true },
      { id: BAD_ID, description: 'Opener', quantity: 1, unit_price: 200, taxable: true },
      { description: 'Half-hour labor', quantity: 0.5, unit_price: 100, taxable: false },
    ]);
    expect(r.warns).toEqual([]);
    const patched = apiPatch.mock.calls.find(([u]) => u.includes(`/lines/${GOOD_ID}`));
    expect(patched[1].quantity).toBe(2.5);
    const posted = apiPost.mock.calls.find(([u]) => u.endsWith('/lines'));
    expect(posted[1].quantity).toBe(0.5);
  });
});
