/**
 * SmsLinkDialog — text a customer the view-and-pay / review-and-approve link.
 *
 * Pins:
 *  1. Opening previews (POST {base}/{id}/sms-preview) and fills To + Message;
 *     nothing is sent until the Send click.
 *  2. A server refusal (`blocked`) is shown and Send stays disabled.
 *  3. Send posts the (edited) number and body to {base}/{id}/send-sms and
 *     emits `sent`; a failure toasts and does not emit.
 *  4. The mobile base is honored for both calls.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiPost = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ post: apiPost }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));

import SmsLinkDialog from '../SmsLinkDialog.vue';

const stubs = {
  Dialog: {
    props: ['visible'],
    emits: ['update:visible'],
    template: '<div v-if="visible"><slot /><slot name="footer" /></div>',
  },
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    template: '<button :data-label="label" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'change'],
    template: '<input data-testid="sms-to" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" @change="$emit(\'change\')" />',
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea data-testid="sms-body" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Message: { template: '<div class="msg"><slot /></div>' },
};

const PREVIEW = {
  to: '+16125550142',
  customer_name: 'Pat Payer',
  body: 'Acme Doors: Invoice #1001 — $250.00 due. View and pay: https://x/pay/tok',
  blocked: null,
};

function mountDialog(props = {}) {
  return mount(SmsLinkDialog, {
    props: { visible: true, docId: 'inv-1', ...props },
    global: { stubs },
  });
}

const sendBtn = (w) => w.find('[data-label="Send text"]');

beforeEach(() => {
  apiPost.mockReset();
  toastAdd.mockReset();
});

describe('SmsLinkDialog', () => {
  it('previews on open and sends nothing until Send', async () => {
    apiPost.mockResolvedValueOnce(PREVIEW);
    const w = mountDialog();
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    expect(apiPost.mock.calls[0][0]).toBe('/api/invoices/inv-1/sms-preview');
    expect(w.find('[data-testid="sms-to"]').element.value).toBe(PREVIEW.to);
    expect(w.find('[data-testid="sms-body"]').element.value).toBe(PREVIEW.body);
    expect(sendBtn(w).attributes('disabled')).toBeUndefined();
  });

  it('shows the refusal and keeps Send disabled', async () => {
    apiPost.mockResolvedValueOnce({
      ...PREVIEW,
      blocked: { code: 'sms_opt_out', message: 'This customer has opted out of text messages.' },
    });
    const w = mountDialog();
    await flushPromises();

    expect(w.find('.msg').text()).toContain('opted out');
    expect(sendBtn(w).attributes('disabled')).toBeDefined();
  });

  it('sends the edited number and body, then emits sent', async () => {
    // Editing the number re-previews (on change), so answer by URL.
    apiPost.mockImplementation(async (url) => (url.endsWith('/sms-preview')
      ? PREVIEW
      : { sms_sent: true, to: '+16125550199' }));
    const w = mountDialog();
    await flushPromises();

    await w.find('[data-testid="sms-to"]').setValue('612-555-0199');
    await w.find('[data-testid="sms-body"]').setValue('Thanks! Pay here');
    await sendBtn(w).trigger('click');
    await flushPromises();

    const [url, payload] = apiPost.mock.calls.at(-1);
    expect(url).toBe('/api/invoices/inv-1/send-sms');
    expect(apiPost.mock.calls.filter((c) => c[0].endsWith('/sms-preview')).at(-1)[1]).toEqual({ to: '612-555-0199' });
    expect(payload).toEqual({ to: '612-555-0199', body: 'Thanks! Pay here', resend_unconfirmed: false });
    expect(w.emitted('sent')).toHaveLength(1);
    expect(w.emitted('update:visible').at(-1)).toEqual([false]);
    expect(toastAdd.mock.calls.at(-1)[0].severity).toBe('success');
  });

  it('a failed send toasts the server message and does not emit sent', async () => {
    apiPost
      .mockResolvedValueOnce(PREVIEW)
      .mockRejectedValueOnce(new Error('Phone.com refused the text: bad number'));
    const w = mountDialog();
    await flushPromises();
    await sendBtn(w).trigger('click');
    await flushPromises();

    expect(w.emitted('sent')).toBeUndefined();
    const toast = toastAdd.mock.calls.at(-1)[0];
    expect(toast.severity).toBe('error');
    expect(toast.detail).toContain('bad number');
  });

  it('uses the mobile base for preview and send', async () => {
    apiPost
      .mockResolvedValueOnce(PREVIEW)
      .mockResolvedValueOnce({ sms_sent: true, to: PREVIEW.to });
    const w = mountDialog({ base: '/api/mobile/invoices' });
    await flushPromises();
    await sendBtn(w).trigger('click');
    await flushPromises();

    expect(apiPost.mock.calls.map((c) => c[0])).toEqual([
      '/api/mobile/invoices/inv-1/sms-preview',
      '/api/mobile/invoices/inv-1/send-sms',
    ]);
  });

  it('an unconfirmed earlier attempt asks before sending again', async () => {
    const refusal = Object.assign(new Error('An earlier text … may have arrived.'), { code: 'prior_attempt_unconfirmed' });
    apiPost
      .mockResolvedValueOnce(PREVIEW)
      .mockRejectedValueOnce(refusal)
      .mockResolvedValueOnce({ sms_sent: true, to: PREVIEW.to });
    const w = mountDialog();
    await flushPromises();

    await sendBtn(w).trigger('click');
    await flushPromises();
    // No error toast, no send: the warning and an explicit "Send anyway".
    expect(toastAdd).not.toHaveBeenCalled();
    expect(w.text()).toContain('may have arrived');
    const anyway = w.find('[data-label="Send anyway"]');
    expect(anyway.exists()).toBe(true);

    await anyway.trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls.at(-1)[1].resend_unconfirmed).toBe(true);
    expect(w.emitted('sent')).toHaveLength(1);
  });

  it('changing the number after the warning drops the "send anyway" decision', async () => {
    const refusal = Object.assign(new Error('An earlier text … may have arrived.'), { code: 'prior_attempt_unconfirmed' });
    apiPost.mockImplementation(async (url, data) => {
      if (url.endsWith('/sms-preview')) return PREVIEW;
      if (!data.resend_unconfirmed) throw refusal;
      return { sms_sent: true, to: data.to };
    });
    const w = mountDialog();
    await flushPromises();
    await sendBtn(w).trigger('click');
    await flushPromises();
    expect(w.find('[data-label="Send anyway"]').exists()).toBe(true);

    await w.find('[data-testid="sms-to"]').setValue('612-555-0177');
    await flushPromises();
    expect(w.find('[data-label="Send anyway"]').exists()).toBe(false);
    await sendBtn(w).trigger('click');
    await flushPromises();
    const last = apiPost.mock.calls.filter((c) => c[0].endsWith('/send-sms')).at(-1)[1];
    expect(last.resend_unconfirmed).toBe(false);
  });

  it('an unconfirmed outcome warns, refreshes the caller and closes', async () => {
    const e = Object.assign(new Error('Phone.com did not confirm the text — it may still have been delivered'), { code: 'sms_outcome_unknown' });
    apiPost.mockResolvedValueOnce(PREVIEW).mockRejectedValueOnce(e);
    const w = mountDialog();
    await flushPromises();
    await sendBtn(w).trigger('click');
    await flushPromises();
    const toast = toastAdd.mock.calls.at(-1)[0];
    expect(toast.severity).toBe('warn');
    expect(toast.summary).toBe('Text not confirmed');
    expect(w.emitted('sent')).toHaveLength(1);
    expect(w.emitted('update:visible').at(-1)).toEqual([false]);
  });
});
