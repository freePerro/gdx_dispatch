/**
 * MobileInvoiceDialog — 2026-07-01 UX audit (field payment capture).
 *
 * Pins:
 *  1. "Record payment" shows only for invoices with balance_due > 0.
 *  2. Recording posts /api/invoices/{id}/payments via the offline queue
 *     with amount/method/date/reference, then reloads the summary.
 *  3. Queued (offline) result → "Payment saved offline" warn toast.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPostQueued = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, postQueued: apiPostQueued }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));
// Deterministic tenant-zone day (and keeps the composable's /api/me/timezone
// fetch out of the apiGet call-count assertions).
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-07-30' }),
}));

// Texting on/off per test (the Text button is gated on the phone_com module).
const smsState = { enabled: true };
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ isEnabled: (k) => (k === 'phone_com' ? smsState.enabled : true) }),
}));

import MobileInvoiceDialog from '../MobileInvoiceDialog.vue';

const stubs = {
  Dialog: {
    props: ['visible'],
    emits: ['update:visible'],
    template: '<div v-if="visible"><slot /><slot name="footer" /></div>',
  },
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'loading', 'disabled', 'size'],
    emits: ['click'],
    template: '<button :data-label="label" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Tag: { props: ['value', 'severity'], template: '<span>{{ value }}</span>' },
  SelectButton: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<div class="sb" />',
  },
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="number" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
};

const SUMMARY = {
  parts_cost: 40,
  labor_hours: 2,
  accepted_quote: { id: 'q1', total: 500 },
  invoices: [
    { id: 'inv-1', invoice_number: '1001', status: 'sent', total: 500, balance_due: 500 },
    { id: 'inv-2', invoice_number: '1002', status: 'paid', total: 300, balance_due: 0 },
  ],
};

function mountDialog() {
  return mount(MobileInvoiceDialog, {
    props: { visible: true, job: { id: 'job-1' } },
    global: { stubs },
  });
}

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue(SUMMARY);
  apiPost.mockReset();
  apiPostQueued.mockReset();
  toastAdd.mockReset();
});

describe('MobileInvoiceDialog — field payment capture', () => {
  it('shows Record payment only for invoices with a balance due', async () => {
    const w = mountDialog();
    // The visible watcher fires on prop set; loadSummary resolves async.
    await flushPromises();
    const payButtons = w.findAll('[data-testid="mid-open-pay"]');
    expect(payButtons).toHaveLength(1); // inv-1 only; inv-2 is paid off
  });

  it('records a payment through the offline queue and reloads the summary', async () => {
    apiPostQueued.mockResolvedValueOnce({ id: 'pay-1' });
    const w = mountDialog();
    await flushPromises();
    await w.get('[data-testid="mid-open-pay"]').trigger('click');
    // Amount prefilled from balance_due (500).
    await w.get('[data-testid="mid-pay-submit"]').trigger('click');
    await flushPromises();

    const [url, payload, opts] = apiPostQueued.mock.calls[0];
    expect(url).toBe('/api/invoices/inv-1/payments');
    expect(payload).toMatchObject({ amount: 500, method: 'cash' });
    // Tenant-zone capture day (mocked) — not a UTC slice.
    expect(payload.date).toBe('2026-07-30');
    expect(opts.actionType).toBe('invoice.payment');
    // Payment 409s are business refusals (void invoice, locked period) —
    // the queue must surface them instead of filing them as synced.
    expect(opts.conflictIsError).toBe(true);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
    // Count the SUMMARY reads, not every GET: the dialog also loads the job's
    // photos for the invoice picker (2026-08-12), and a bare call count turns
    // any future read into a failure of this assertion.
    const summaryCalls = apiGet.mock.calls.filter(([u]) => String(u).includes('/financial'));
    expect(summaryCalls).toHaveLength(2); // initial + reload after payment
  });

  it('refused payment (409 business refusal) shows an error toast, not success', async () => {
    const refusal = new Error('invoice is void — un-void it before recording a payment');
    refusal.status = 409;
    apiPostQueued.mockRejectedValueOnce(refusal);
    const w = mountDialog();
    await flushPromises();
    await w.get('[data-testid="mid-open-pay"]').trigger('click');
    await w.get('[data-testid="mid-pay-submit"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'error',
        summary: 'Could not record payment',
        detail: 'invoice is void — un-void it before recording a payment',
      })
    );
    expect(toastAdd).not.toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success' })
    );
  });

  it('offline (queued) payment shows the saved-offline warn toast', async () => {
    apiPostQueued.mockResolvedValueOnce({ queued: true, idempotency_key: 'k' });
    const w = mountDialog();
    await flushPromises();
    await w.get('[data-testid="mid-open-pay"]').trigger('click');
    await w.get('[data-testid="mid-pay-submit"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn', summary: 'Payment saved offline' })
    );
  });
});

/**
 * Job photos on the invoice, picked from the truck (2026-08-12).
 *
 * The tech shot the photos minutes ago and is about to email the bill — on a
 * send_email invoice this dialog is the last moment anyone can put the
 * finished-door shot on it before the customer reads it. Before this there was
 * no photo affordance on mobile at all.
 */
describe('MobileInvoiceDialog — job photos', () => {
  const PHOTOS = [
    { id: 'ph-1', url: '/api/documents/d1/download', kind: 'before' },
    { id: 'ph-2', url: '/api/documents/d2/download', kind: 'after' },
  ];

  it('sends the ticked photos with the generated invoice', async () => {
    apiGet.mockImplementation((url) => {
      if (String(url).includes('/photos')) return Promise.resolve(PHOTOS);
      return Promise.resolve({ ...SUMMARY, invoices: [] });
    });
    apiPost.mockResolvedValue({ invoice_number: 'INV-1' });

    const w = mountDialog();
    await flushPromises();

    await w.get('[data-testid="mid-photo-ph-2"] input').setValue(true);
    await flushPromises();
    await w.get('[data-testid="mid-generate"]').trigger('click');
    await flushPromises();

    const [, payload] = apiPost.mock.calls[0];
    expect(payload.attached_photo_ids).toEqual(['ph-2']);
  });

  it('shows no picker when the job has no photos', async () => {
    apiGet.mockImplementation((url) => {
      if (String(url).includes('/photos')) return Promise.resolve([]);
      return Promise.resolve({ ...SUMMARY, invoices: [] });
    });
    const w = mountDialog();
    await flushPromises();
    expect(w.find('[data-testid="mid-photos"]').exists()).toBe(false);
  });

  it('a failed photo read never blocks invoicing from the truck', async () => {
    apiGet.mockImplementation((url) => {
      if (String(url).includes('/photos')) return Promise.reject(new Error('offline'));
      return Promise.resolve({ ...SUMMARY, invoices: [] });
    });
    const w = mountDialog();
    await flushPromises();
    expect(w.find('[data-testid="mid-photos"]').exists()).toBe(false);
    expect(w.find('[data-testid="mid-generate"]').exists()).toBe(true);
  });
});

describe('sendReceipt reads the REAL outcome (2026-08-18)', () => {
  // The backend stopped hardcoding {"sent": true}; a toast that ignores the
  // response would re-create the fake-success defect one layer up.
  const { readFileSync } = require('node:fs')
  const { join } = require('node:path')
  const SRC = readFileSync(join(__dirname, '..', 'MobileInvoiceDialog.vue'), 'utf8')

  it('branches on payload.sent and surfaces skip_reason on failure', () => {
    const start = SRC.indexOf('async function sendReceipt')
    expect(start).toBeGreaterThan(-1)
    const end = SRC.indexOf('</script>', start)
    const body = SRC.slice(start, end)
    expect(body).toContain('payload?.sent')
    expect(body).toContain('skip_reason')
    expect(body).toContain('Receipt NOT sent')
    // Success toast must be inside the sent branch, not unconditional.
    expect(body.indexOf('payload?.sent')).toBeLessThan(body.indexOf("summary: 'Receipt sent'"))
  })
})

describe('M38 already_billed 409', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // No billing-real invoice in the summary — the Generate button only
    // renders on an unbilled job. The 409 is precisely the race where the
    // SERVER knows about an invoice this stale dialog doesn't.
    apiGet.mockResolvedValue({ ...JSON.parse(JSON.stringify(SUMMARY)), invoices: [] });
  });

  it('a double-tap 409 shows the already-billed warn toast and refreshes — never the raw error toast', async () => {
    const err = new Error('Job is already billed on INV-000123.');
    err.status = 409;
    err.code = 'already_billed';
    apiPost.mockRejectedValueOnce(err);
    const w = mountDialog();
    await flushPromises();
    const getCallsBefore = apiGet.mock.calls.length;
    const btn = w.findAll('button').find((b) => (b.attributes('data-label') || '').startsWith('Generate'));
    expect(btn).toBeTruthy();
    await btn.trigger('click');
    await flushPromises();
    const toasts = toastAdd.mock.calls.map((c) => c[0]);
    const warn = toasts.find((t) => t.severity === 'warn' && t.summary === 'Already billed');
    expect(warn, 'the already_billed branch must produce the warn toast').toBeTruthy();
    expect(warn.detail).toContain('INV-000123');
    expect(toasts.find((t) => t.summary === 'Could not invoice')).toBeFalsy();
    // and the dialog refreshed so the existing invoice renders
    expect(apiGet.mock.calls.length).toBeGreaterThan(getCallsBefore);
  });

  it('any other failure still shows the plain error toast', async () => {
    const err = new Error('boom');
    err.status = 500;
    apiPost.mockRejectedValueOnce(err);
    const w = mountDialog();
    await flushPromises();
    const btn = w.findAll('button').find((b) => (b.attributes('data-label') || '').startsWith('Generate'));
    await btn.trigger('click');
    await flushPromises();
    const toasts = toastAdd.mock.calls.map((c) => c[0]);
    expect(toasts.find((t) => t.summary === 'Could not invoice')).toBeTruthy();
    expect(toasts.find((t) => t.summary === 'Already billed')).toBeFalsy();
  });
});

describe('MobileInvoiceDialog — Text button placement (2026-09-28)', () => {
  const verified = (over) => ({
    id: 'inv-v', invoice_number: '2001', status: 'sent', total: 300, balance_due: 300,
    verified_at: '2026-09-28T12:00:00Z', ...over,
  });
  const mountWith = async (inv, sms) => {
    smsState.enabled = sms;
    apiGet.mockReset().mockResolvedValue({ ...SUMMARY, invoices: [inv] });
    const w = mountDialog();
    await flushPromises();
    return w;
  };

  it.each([
    ['texting off', verified(), false],
    ['nothing owed', verified({ balance_due: 0, status: 'paid' }), true],
    ['void', verified({ status: 'void' }), true],
  ])('a verified invoice never shows the "unlocks when verified" note (%s)', async (_label, inv, sms) => {
    // The Text button once sat between Re-send's v-if and this v-else, so the
    // v-else attached to Text and the note appeared on verified invoices.
    const w = await mountWith(inv, sms);
    expect(w.find('[data-label="Re-send"]').exists()).toBe(true);
    expect(w.find('[data-testid="mid-send-blocked"]').exists()).toBe(false);
    expect(w.find('[data-testid="mid-text-invoice"]').exists()).toBe(false);
  });

  it('shows Text for a verified invoice with a balance when texting is on', async () => {
    const w = await mountWith(verified(), true);
    expect(w.find('[data-testid="mid-text-invoice"]').exists()).toBe(true);
    expect(w.find('[data-testid="mid-send-blocked"]').exists()).toBe(false);
  });

  it('an unverified invoice shows the note and no Text', async () => {
    const w = await mountWith(verified({ verified_at: null }), true);
    expect(w.find('[data-testid="mid-send-blocked"]').exists()).toBe(true);
    expect(w.find('[data-testid="mid-text-invoice"]').exists()).toBe(false);
  });
});

/**
 * Re-send and receipt read the delivery result (GDXA-1278). The server now
 * suppresses a second send inside its window; a toast that ignored that
 * would tell the tech "re-sent" when nothing went out.
 */
describe('MobileInvoiceDialog — send outcomes', () => {
  const VERIFIED = {
    ...SUMMARY,
    invoices: [{ id: 'inv-1', invoice_number: '1001', status: 'sent', total: 500, balance_due: 500, verified_at: '2026-10-16T00:00:00Z' }],
  };

  async function clickLabel(w, label) {
    await w.get(`[data-label="${label}"]`).trigger('click');
    await flushPromises();
  }

  it.each([
    [{ email_sent: true }, 'success', 'Invoice re-sent'],
    [{ email_sent: false, email_skip_reason: 'duplicate_send_suppressed' }, 'info', 'Already sent'],
    [{ email_sent: false }, 'warn', 'Email not delivered'],
  ])('re-send %j toasts %s', async (resp, severity, summary) => {
    apiGet.mockResolvedValue(VERIFIED);
    apiPost.mockResolvedValueOnce(resp);
    const w = mountDialog();
    await flushPromises();
    await clickLabel(w, 'Re-send');
    expect(apiPost.mock.calls[0][0]).toBe('/api/mobile/invoices/inv-1/send');
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity, summary }));
    expect(toastAdd).toHaveBeenCalledTimes(1);
  });

  it('a suppressed duplicate receipt is "Already sent", not an error', async () => {
    apiGet.mockResolvedValue(VERIFIED);
    apiPost.mockResolvedValueOnce({ sent: false, skip_reason: 'duplicate_send_suppressed' });
    const w = mountDialog();
    await flushPromises();
    await clickLabel(w, 'Send receipt');
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'info', summary: 'Already sent' }));
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ severity: 'error' }));
  });
});

describe('MobileInvoiceDialog — Re-send with files (2026-10-16)', () => {
  // The picker's own loading is pinned by EmailAttachmentPicker.spec.js; here
  // it only has to show who it was opened for.
  const Picker = {
    props: ['modelValue', 'jobId', 'customerId', 'disabled'],
    template: '<div data-testid="picker-stub" :data-job="jobId" :data-customer="customerId" />',
  };
  const inv = {
    id: 'inv-v', invoice_number: '2001', status: 'sent', total: 300, balance_due: 300,
    customer_id: 'cust-9', verified_at: '2026-09-28T12:00:00Z',
  };
  const PHOTO = { kind: 'job_photo', id: 'ph-1', name: 'door.jpg', size_bytes: 1200 };

  const open = async () => {
    apiGet.mockReset().mockResolvedValue({ ...SUMMARY, invoices: [inv] });
    const w = mount(MobileInvoiceDialog, {
      props: { visible: true, job: { id: 'job-1' } },
      global: { stubs: { ...stubs, EmailAttachmentPicker: Picker } },
    });
    await flushPromises();
    return w;
  };
  const resend = (w) => w.find('[data-testid="mid-resend"]');

  it('Add files opens the picker for this job and customer, and the picks ride on Re-send', async () => {
    const w = await open();
    expect(w.find('[data-testid="picker-stub"]').exists()).toBe(false);
    await w.find('[data-testid="mid-resend-attach"]').trigger('click');
    const picker = w.find('[data-testid="picker-stub"]');
    expect(picker.attributes('data-job')).toBe('job-1');
    expect(picker.attributes('data-customer')).toBe('cust-9');

    w.vm.$.setupState.resendPicked = [PHOTO];
    await flushPromises();
    expect(resend(w).text()).toBe('Re-send with 1 file');
    apiPost.mockResolvedValueOnce({ id: 'inv-v', email_sent: true });
    await resend(w).trigger('click');
    await flushPromises();

    const [url, body, opts] = apiPost.mock.calls[0];
    expect(url).toBe('/api/mobile/invoices/inv-v/send');
    expect(body).toEqual({ attachment_refs: [{ kind: 'job_photo', id: 'ph-1' }] });
    // A refused pick is said once by this dialog; only a 401 is left to useApi.
    expect(opts.suppressErrorToast({ status: 404 })).toBe(true);
    expect(opts.suppressErrorToast({ status: 401 })).toBe(false);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success', summary: 'Invoice re-sent' }));
    expect(w.find('[data-testid="picker-stub"]').exists()).toBe(false);
  });

  it('warns when the picks left the invoice PDF out', async () => {
    const w = await open()
    await w.find('[data-testid="mid-resend-attach"]').trigger('click')
    w.vm.$.setupState.resendPicked = [PHOTO]
    await flushPromises()
    apiPost.mockResolvedValueOnce({ id: 'inv-v', email_sent: true, pdf_attached: false })
    await resend(w).trigger('click')
    await flushPromises()
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'warn', summary: 'Invoice re-sent without its PDF',
      detail: expect.stringContaining('invoice PDF was not attached'),
    }))
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: 'Invoice re-sent' }))
  })

  it('a plain Re-send whose PDF did not go does not blame files nobody added', async () => {
    const w = await open()
    apiPost.mockResolvedValueOnce({ id: 'inv-v', email_sent: true, pdf_attached: false })
    await resend(w).trigger('click')
    await flushPromises()
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: 'Invoice re-sent without its PDF' }))
  })

  it('a plain Re-send posts no attachment_refs', async () => {
    const w = await open();
    apiPost.mockResolvedValueOnce({ id: 'inv-v', email_sent: true });
    await resend(w).trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][1]).toEqual({});
  });

  it('never shows "re-sent" over an email that did not go, files or not', async () => {
    const w = await open();
    await w.find('[data-testid="mid-resend-attach"]').trigger('click');
    w.vm.$.setupState.resendPicked = [PHOTO];
    await flushPromises();
    apiPost.mockResolvedValueOnce({ id: 'inv-v', email_sent: false, pdf_attached: false });
    await resend(w).trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledTimes(1);
    expect(toastAdd.mock.calls[0][0]).toMatchObject({ severity: 'warn', summary: 'Email not delivered' });
  });

  it('a suppressed repeat with files says "Already sent" and keeps the picks', async () => {
    const w = await open();
    await w.find('[data-testid="mid-resend-attach"]').trigger('click');
    w.vm.$.setupState.resendPicked = [PHOTO];
    await flushPromises();
    apiPost.mockResolvedValueOnce({ id: 'inv-v', email_sent: false, email_skip_reason: 'duplicate_send_suppressed' });
    await resend(w).trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledTimes(1);
    expect(toastAdd.mock.calls[0][0]).toMatchObject({ severity: 'info', summary: 'Already sent' });
    expect(w.find('[data-testid="picker-stub"]').exists()).toBe(true);
    expect(resend(w).text()).toBe('Re-send with 1 file');
  });

  it('a refused pick says why, once; a 401 is left to the sign-in flow', async () => {
    const w = await open();
    await w.find('[data-testid="mid-resend-attach"]').trigger('click');
    w.vm.$.setupState.resendPicked = [PHOTO];
    apiPost.mockRejectedValueOnce(Object.assign(new Error('Attachment not found: door.jpg'), { status: 404 }));
    await resend(w).trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledTimes(1);
    expect(toastAdd.mock.calls[0][0]).toMatchObject({ severity: 'error', detail: 'Attachment not found: door.jpg' });

    toastAdd.mockReset();
    apiPost.mockRejectedValueOnce(Object.assign(new Error('Unauthorized'), { status: 401 }));
    await resend(w).trigger('click');
    await flushPromises();
    expect(toastAdd).not.toHaveBeenCalled();
  });

  it('a dropped connection warns that it may have gone', async () => {
    const w = await open();
    apiPost.mockRejectedValueOnce(new Error('Failed to fetch'));
    await resend(w).trigger('click');
    await flushPromises();
    expect(toastAdd.mock.calls[0][0].detail).toContain('Check Sent Items before sending again');
  });

  it('Re-send is disabled while the picks are over the email cap', async () => {
    const w = await open();
    await w.find('[data-testid="mid-resend-attach"]').trigger('click');
    w.vm.$.setupState.resendPicked = [{ ...PHOTO, size_bytes: 19_000_000 }];
    await flushPromises();
    expect(resend(w).attributes('disabled')).toBeDefined();
  });
});
