/**
 * CustomerStatementDialog.
 *
 * Pinned contract:
 *  - Opening loads the default preset (last 90 days) and the PDF preview for
 *    the SAME query, so the preview and the numbers can't cover different days.
 *  - Warnings the server returns are shown; they are the office's only guard
 *    against figures built on payment records that don't add up.
 *  - Send posts the preset and the typed address, and a failed send says why
 *    in words — never a silent or green result.
 *  - The Email button exists only with invoices.send.
 *  - A custom range goes to the server as local calendar days, not UTC.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const hasPermission = vi.fn(() => true);
const createAuthedBlobUrl = vi.fn(async () => 'blob:preview');
const downloadAuthedFile = vi.fn(async () => {});

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({ hasPermission }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  createAuthedBlobUrl: (...a) => createAuthedBlobUrl(...a),
  downloadAuthedFile: (...a) => downloadAuthedFile(...a),
}));

import CustomerStatementDialog from '../CustomerStatementDialog.vue';
import { recipientPayload, skipReasonMessage, statementParams, toIsoDay } from '../../utils/customerStatement';

const stubs = {
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible"><slot /></div>',
  },
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    inheritAttrs: false,
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    inheritAttrs: false,
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Message: { inheritAttrs: false, template: '<div :data-testid="$attrs[\'data-testid\']"><slot /></div>' },
  Select: true,
  ProgressSpinner: true,
};

function statement(overrides = {}) {
  return {
    range: { start: '2026-06-18', end: '2026-09-15', preset: 'last_90' },
    ends_today: true,
    total_unpaid: 1450,
    credit_on_account: 0,
    previous_balance: 1200,
    ending_balance: 1450,
    open_invoices: [{ invoice_number: 'INV-1' }],
    warnings: [],
    presets: [{ key: 'last_90', label: 'Last 90 days' }, { key: 'custom', label: 'Custom range' }],
    default_recipient: { email: 'owner@example.com' },
    pdf_filename: 'statement-2026-06-18-to-2026-09-15.pdf',
    ...overrides,
  };
}

async function open(data = statement()) {
  apiGet.mockResolvedValueOnce(data);
  const w = mount(CustomerStatementDialog, { props: { visible: true, customerId: 'c-1' }, global: { stubs } });
  await flushPromises();
  return w;
}

describe('CustomerStatementDialog', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    createAuthedBlobUrl.mockClear();
    downloadAuthedFile.mockClear();
    hasPermission.mockReset();
    hasPermission.mockReturnValue(true);
  });

  it('loads the default preset and previews the PDF for the same query', async () => {
    const w = await open();
    expect(apiGet).toHaveBeenCalledWith('/api/customers/c-1/statement?preset=last_90');
    expect(createAuthedBlobUrl).toHaveBeenCalledWith('/api/customers/c-1/statement/pdf?preset=last_90');
    expect(w.find('[data-testid="statement-total-unpaid"]').text()).toBe('$1,450.00');
    expect(w.find('[data-testid="statement-to-email"]').element.value).toBe('owner@example.com');
    expect(w.find('[data-testid="statement-preview"]').attributes('src')).toBe('blob:preview');
  });

  it('shows every warning the server returns', async () => {
    const w = await open(statement({
      warnings: [{ kind: 'records_dont_add_up', invoice_id: 'i1', invoice_number: '49908124', message: "don't add up" }],
    }));
    const warnings = w.findAll('[data-testid="statement-warning"]');
    expect(warnings).toHaveLength(1);
    expect(warnings[0].text()).toContain('49908124');
  });

  it('sends the preset and typed address, and explains a failed send', async () => {
    const w = await open();
    await w.find('[data-testid="statement-to-email"]').setValue('ap@builder.example');
    apiPost.mockResolvedValueOnce({ email_sent: false, email_skip_reason: 'no_email_provider_connected', to_email: 'ap@builder.example' });
    await w.find('[data-testid="statement-send"]').trigger('click');
    await flushPromises();
    // The previewed dates, not the preset key: re-resolving "last_90" after
    // midnight would send a different range than the one on screen.
    expect(apiPost).toHaveBeenCalledWith('/api/customers/c-1/statement/send', { start: '2026-06-18', end: '2026-09-15', to_email: 'ap@builder.example' });
    expect(w.find('[data-testid="statement-result"]').text()).toContain('No email account is connected');
  });

  it('leaves the prefilled recipient to the server, so a contact keeps their greeting', async () => {
    const w = await open();
    apiPost.mockResolvedValueOnce({ email_sent: true, to_email: 'owner@example.com' });
    await w.find('[data-testid="statement-send"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith('/api/customers/c-1/statement/send', { start: '2026-06-18', end: '2026-09-15' });
  });

  it('confirms a successful send by address', async () => {
    const w = await open();
    apiPost.mockResolvedValueOnce({ email_sent: true, pdf_attached: true, to_email: 'owner@example.com' });
    await w.find('[data-testid="statement-send"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="statement-result"]').text()).toContain('emailed to owner@example.com with the PDF attached');
  });

  it('hides Email without invoices.send but still offers the download', async () => {
    hasPermission.mockImplementation((key) => key !== 'invoices.send');
    const w = await open();
    expect(w.find('[data-testid="statement-send"]').exists()).toBe(false);
    await w.find('[data-testid="statement-download"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile).toHaveBeenCalledWith(
      '/api/customers/c-1/statement/pdf?start=2026-06-18&end=2026-09-15&download=true',
      'statement-2026-06-18-to-2026-09-15.pdf',
    );
  });

  it('loads a custom range only when Show is pressed with both dates set', async () => {
    const w = await open();
    apiGet.mockReset();
    apiGet.mockResolvedValue(statement({ range: { start: '2026-05-01', end: '2026-07-31', preset: 'custom' } }));
    w.vm.$.setupState.preset = 'custom';
    await flushPromises();
    const apply = () => w.find('[data-testid="statement-apply"]');
    expect(apply().attributes('disabled')).toBeDefined();
    // Nothing loads while the dates are being entered.
    await w.find('[data-testid="statement-start"]').setValue('2026-05-01');
    await w.find('[data-testid="statement-end"]').setValue('2026-07-03');
    await flushPromises();
    await w.find('[data-testid="statement-end"]').setValue('2026-07-31');
    await flushPromises();
    expect(apiGet).not.toHaveBeenCalled();
    await apply().trigger('click');
    await flushPromises();
    expect(apiGet).toHaveBeenCalledTimes(1);
    expect(apiGet).toHaveBeenCalledWith('/api/customers/c-1/statement?start=2026-05-01&end=2026-07-31');
  });

  it('never downloads or emails a range that is not the one on screen', async () => {
    const w = await open();
    apiGet.mockReset();
    apiGet.mockResolvedValue(statement({ range: { start: '2026-05-01', end: '2026-07-31', preset: 'custom' } }));
    // Switching to Custom range: the last-90-days preview is still showing, so
    // both actions wait for Show — disabled with a reason, not a dead click.
    w.vm.$.setupState.preset = 'custom';
    await flushPromises();
    expect(w.find('[data-testid="statement-stale"]').exists()).toBe(true);
    expect(w.find('[data-testid="statement-send"]').attributes('disabled')).toBeDefined();
    expect(w.find('[data-testid="statement-download"]').attributes('disabled')).toBeDefined();

    await w.find('[data-testid="statement-start"]').setValue('2026-05-01');
    await w.find('[data-testid="statement-end"]').setValue('2026-07-31');
    await w.find('[data-testid="statement-apply"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="statement-stale"]').exists()).toBe(false);

    // Edit the range after previewing it: sending must not go out for it.
    await w.find('[data-testid="statement-start"]').setValue('2026-01-01');
    await flushPromises();
    expect(w.find('[data-testid="statement-send"]').attributes('disabled')).toBeDefined();
    await w.vm.$.setupState.send();
    await w.vm.$.setupState.download();
    expect(apiPost).not.toHaveBeenCalled();
    expect(downloadAuthedFile).not.toHaveBeenCalled();

    // Back to what is on screen: the send carries exactly the previewed range.
    await w.find('[data-testid="statement-start"]').setValue('2026-05-01');
    await flushPromises();
    apiPost.mockResolvedValueOnce({ email_sent: true, to_email: 'owner@example.com' });
    await w.find('[data-testid="statement-send"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith('/api/customers/c-1/statement/send', { start: '2026-05-01', end: '2026-07-31' });
  });

  it('shows the server refusal instead of a blank dialog', async () => {
    apiGet.mockRejectedValueOnce(new Error('statements start on 2026-01-01 or later'));
    const w = mount(CustomerStatementDialog, { props: { visible: true, customerId: 'c-1' }, global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="statement-error"]').text()).toContain('2026-01-01');
  });
});

describe('customerStatement helpers', () => {
  it('turns a picked day into YYYY-MM-DD on the local calendar', () => {
    // toISOString() would put a US 11:30pm on the next day.
    expect(toIsoDay(new Date(2026, 0, 1))).toBe('2026-01-01');
    expect(toIsoDay(new Date(2026, 11, 31, 23, 30))).toBe('2026-12-31');
    expect(toIsoDay(null)).toBe('');
    expect(toIsoDay('2026-07-31')).toBe('2026-07-31'); // a native date input's value
    expect(toIsoDay('2026-07-3')).toBe(''); // never a half-typed day
  });

  it('builds a custom range only when both days are picked', () => {
    expect(statementParams('custom', new Date(2026, 2, 1), null)).toBeNull();
    expect(statementParams('custom', new Date(2026, 2, 1), new Date(2026, 3, 30))).toEqual({ start: '2026-03-01', end: '2026-04-30' });
    expect(statementParams('ytd')).toEqual({ preset: 'ytd' });
    expect(statementParams(undefined)).toEqual({ preset: 'last_90' });
  });

  it('sends an address only when the operator changed it', () => {
    expect(recipientPayload('owner@example.com', 'owner@example.com')).toEqual({});
    expect(recipientPayload(' Owner@Example.com ', 'owner@example.com')).toEqual({});
    expect(recipientPayload('ap@builder.example', 'owner@example.com')).toEqual({ to_email: 'ap@builder.example' });
    expect(recipientPayload('ap@builder.example', '')).toEqual({ to_email: 'ap@builder.example' });
  });

  it('never leaves a skip reason unexplained', () => {
    expect(skipReasonMessage('customer_has_no_email')).toMatch(/no email address/);
    expect(skipReasonMessage('something_new')).toContain('something_new');
    expect(skipReasonMessage(undefined)).toBe('The statement was not sent.');
  });
});
