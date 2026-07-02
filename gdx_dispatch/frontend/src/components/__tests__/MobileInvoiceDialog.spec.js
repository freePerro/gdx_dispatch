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
    expect(payload.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(opts.actionType).toBe('invoice.payment');
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
    expect(apiGet).toHaveBeenCalledTimes(2); // initial + reload after payment
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
