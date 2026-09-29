/**
 * MobileCustomerQuoteDialog — "Text" the approval link (2026-09-29).
 *
 * The customer standing at the door who wants to think it over: the tech
 * texts them the estimate instead. Pins:
 *  1. Text shows for a live quote when texting (phone_com) is on.
 *  2. It hides for accepted / declined quotes and when texting is off.
 *  3. It opens the shared SmsLinkDialog against the TECH endpoint
 *     (/api/mobile/quotes) — techs hold no estimates.send, so the office
 *     /api/estimates route would 403 (docs/tech_mobile.md).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const smsState = { enabled: true };
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ reasons: [] }), post: vi.fn(), postQueued: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ isEnabled: (k) => (k === 'phone_com' ? smsState.enabled : true) }),
}));

import MobileCustomerQuoteDialog from '../MobileCustomerQuoteDialog.vue';

const SmsLinkDialogStub = {
  name: 'SmsLinkDialog',
  props: ['visible', 'docId', 'base', 'title'],
  template: '<div data-testid="sms-stub" :data-visible="String(visible)" />',
};

const stubs = {
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button :data-label="label" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  InputText: true, Tag: true, Select: true, Textarea: true, PaymentCaptureForm: true,
  SmsLinkDialog: SmsLinkDialogStub,
};

const mountWith = (quote) => mount(MobileCustomerQuoteDialog, {
  props: { visible: true, quote: { tiers: [], ...quote } },
  global: { stubs },
});

beforeEach(() => { smsState.enabled = true; });

describe('MobileCustomerQuoteDialog — Text', () => {
  it('shows Text for a live quote and opens the tech SMS dialog', async () => {
    const w = mountWith({ id: 'q1', status: 'sent' });
    await flushPromises();
    const btn = w.find('[data-testid="mobile-quote-text"]');
    expect(btn.exists()).toBe(true);

    const dlg = w.findComponent(SmsLinkDialogStub);
    expect(dlg.props('base')).toBe('/api/mobile/quotes');
    expect(dlg.props('docId')).toBe('q1');
    expect(dlg.props('visible')).toBe(false);
    await btn.trigger('click');
    expect(w.findComponent(SmsLinkDialogStub).props('visible')).toBe(true);
  });

  it.each([
    ['accepted', { id: 'q2', status: 'accepted' }, true],
    ['declined', { id: 'q3', status: 'declined' }, true],
    ['texting off', { id: 'q4', status: 'sent' }, false],
  ])('hides Text when %s', async (_label, quote, sms) => {
    smsState.enabled = sms;
    const w = mountWith(quote);
    await flushPromises();
    expect(w.find('[data-testid="mobile-quote-text"]').exists()).toBe(false);
  });

  it('passes the texted quote up so the job view shows it as sent', async () => {
    const w = mountWith({ id: 'q5', status: 'draft' });
    await flushPromises();
    w.findComponent(SmsLinkDialogStub).vm.$emit('sent', { id: 'q5', status: 'sent', sms_sent: true });
    expect(w.emitted('texted')).toEqual([[{ id: 'q5', status: 'sent', sms_sent: true }]]);
  });
});
