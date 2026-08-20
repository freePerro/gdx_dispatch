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

  it('verify is NOT gated behind a confirm — useDestructiveConfirm auto-accepts (#215)', () => {
    const i = SRC.indexOf('async function verifyInvoice');
    const span = SRC.slice(i, i + 900);
    expect(span).not.toMatch(/confirmAsync/);
    expect(span).toMatch(/#215/);
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

