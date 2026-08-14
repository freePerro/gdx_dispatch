/**
 * Public estimate approval page (/proposals/:token) — the QuickBooks moment.
 *
 * Pinned:
 *  1. Line estimates show the tax-inclusive totals block the backend computed;
 *     the page never does its own money math.
 *  2. proposal_mode shows PER-TIER prices only (the backend omits `totals` on
 *     purpose — the totals engine is tier-blind and est.total can be the
 *     highest tier). Accept is gated on picking a tier, and the POST carries
 *     the picked tier_id.
 *  3. A deposit in the accept response opens the one-motion pay prompt; an
 *     accepted estimate with a still-owed deposit shows the pay button on
 *     plain load too (accept-then-abandon recovery).
 *  4. A bad token renders the friendly dead-end, not a blank page.
 *  5. Decline posts the optional reason only when one was typed.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {}, params: { token: 'tok-abc' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import ProposalPublicView from '../ProposalPublicView.vue';

const LINE_PAYLOAD = {
  company: { name: 'Garage Door Xperts', phone: '555-0100' },
  estimate: {
    estimate_number: 'EST-000123', label: '16x7 insulated door', description: 'Replace door',
    jobsite_address: '1 Main St', status: 'sent', proposal_mode: false, hide_line_prices: false,
    valid_until: '2026-09-12T00:00:00Z', sent_at: '2026-08-13T00:00:00Z',
    accepted_at: null, declined_at: null, accepted_tier_id: null,
  },
  tiers: [],
  lines: [{ description: '16x7 door', quantity: 1, unit_price: 2600, line_total: 2600 }],
  totals: { subtotal: 2600, discount: 0, tax: 182, tax_rate_pct: 7, total: 2782 },
  deposit_pct: 50,
};

const TIER_PAYLOAD = {
  ...LINE_PAYLOAD,
  estimate: { ...LINE_PAYLOAD.estimate, proposal_mode: true },
  tiers: [
    { id: 't-good', tier_name: 'good', description: 'Base', total_price: 2500, includes_parts: true, warranty_months: 12, display_order: 1 },
    { id: 't-best', tier_name: 'best', description: 'Premium', total_price: 8000, includes_parts: true, warranty_months: 60, display_order: 2 },
  ],
  lines: [],
  totals: undefined,
};

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'outlined', 'size', 'loading', 'disabled'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  Card: { template: '<div :data-testid="$attrs[\'data-testid\']"><slot name="title" /><slot name="subtitle" /><slot name="content" /></div>', inheritAttrs: false },
  Tag: { props: ['value', 'severity'], template: '<span :data-testid="$attrs[\'data-testid\']">{{ value }}</span>', inheritAttrs: false },
  Message: { props: ['severity', 'closable'], template: '<div :data-testid="$attrs[\'data-testid\']"><slot /></div>', inheritAttrs: false },
  Dialog: {
    props: ['visible', 'header', 'modal'],
    emits: ['update:visible'],
    template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>',
    inheritAttrs: false,
  },
  DataTable: { props: ['value'], template: '<div :data-testid="$attrs[\'data-testid\']" :data-rows="value.length" />', inheritAttrs: false },
  Column: { template: '<div />' },
  ProgressSpinner: { template: '<div />' },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
};

function mockFetch(routes) {
  global.fetch = vi.fn((url, opts = {}) => {
    const key = `${opts.method || 'GET'} ${url}`;
    const hit = routes[key];
    if (!hit) return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ detail: 'Invalid proposal token' }) });
    const body = typeof hit === 'function' ? hit(opts) : hit;
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  });
  return global.fetch;
}

async function mountPage() {
  const wrapper = mount(ProposalPublicView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  toastAdd.mockClear();
});

describe('ProposalPublicView', () => {
  it('renders a line estimate with the backend-computed tax-inclusive total', async () => {
    mockFetch({ 'GET /api/proposals/tok-abc': LINE_PAYLOAD });
    const w = await mountPage();

    expect(w.find('[data-testid="grand-total"]').text()).toContain('2,782');
    expect(w.find('[data-testid="lines-table"]').attributes('data-rows')).toBe('1');
    expect(w.find('[data-testid="accept-btn"]').exists()).toBe(true);
    expect(w.find('[data-testid="decline-btn"]').exists()).toBe(true);
    expect(w.text()).toContain('Garage Door Xperts');
  });

  it('proposal_mode: per-tier prices, accept gated on a pick, tier_id in the POST', async () => {
    const fetch = mockFetch({
      'GET /api/proposals/tok-abc': TIER_PAYLOAD,
      'POST /api/proposals/tok-abc/accept': (opts) => {
        expect(JSON.parse(opts.body)).toEqual({ tier_id: 't-best' });
        return {
          ...TIER_PAYLOAD,
          estimate: { ...TIER_PAYLOAD.estimate, status: 'accepted', accepted_tier_id: 't-best' },
          deposit: { invoice_number: 'INV-9', amount: 4000, balance_due: 4000, pay_url: 'https://x/pay/tok', status: 'sent', pct: 50 },
        };
      },
    });
    const w = await mountPage();

    // No combined total anywhere in tier mode.
    expect(w.find('[data-testid="totals-block"]').exists()).toBe(false);
    expect(w.find('[data-testid="tier-best"]').text()).toContain('8,000');
    expect(w.find('[data-testid="accept-btn"]').attributes('disabled')).toBeDefined();

    await w.find('[data-testid="tier-best"]').trigger('click');
    expect(w.find('[data-testid="accept-btn"]').attributes('disabled')).toBeUndefined();

    await w.find('[data-testid="accept-btn"]').trigger('click');
    await w.find('[data-testid="accept-confirm"]').trigger('click');
    await flushPromises();

    expect(fetch).toHaveBeenCalledWith('/api/proposals/tok-abc/accept', expect.objectContaining({ method: 'POST' }));
    // One-motion deposit prompt opens off the accept response.
    expect(w.find('[data-testid="deposit-pay-dialog"]').exists()).toBe(true);
    expect(w.find('[data-testid="deposit-pay-dialog"]').text()).toContain('4,000');
  });

  it('line-built tiers list their included items on the card', async () => {
    mockFetch({
      'GET /api/proposals/tok-abc': {
        ...TIER_PAYLOAD,
        tiers: [
          {
            ...TIER_PAYLOAD.tiers[1],
            lines: [
              { description: 'Belt drive opener', quantity: 1, unit_price: 6000, line_total: 6000 },
              { description: 'Battery backup', quantity: 2, unit_price: 1000, line_total: 2000 },
            ],
          },
        ],
      },
    });
    const w = await mountPage();

    const included = w.find('[data-testid="tier-lines-best"]');
    expect(included.exists()).toBe(true);
    expect(included.text()).toContain('Belt drive opener');
    expect(included.text()).toContain('2× Battery backup');
    expect(included.text()).toContain('2,000');
  });

  it('accepted estimate with a still-owed deposit shows the pay button on load', async () => {
    mockFetch({
      'GET /api/proposals/tok-abc': {
        ...LINE_PAYLOAD,
        estimate: { ...LINE_PAYLOAD.estimate, status: 'accepted', accepted_at: '2026-08-13T01:00:00Z' },
        deposit: { invoice_number: 'INV-9', amount: 1300, balance_due: 1300, pay_url: 'https://x/pay/tok', status: 'sent' },
      },
    });
    const w = await mountPage();

    expect(w.find('[data-testid="accepted-msg"]').exists()).toBe(true);
    expect(w.find('[data-testid="pay-deposit-btn"]').text()).toContain('1,300');
    expect(w.find('[data-testid="accept-btn"]').exists()).toBe(false);
  });

  it('bad token renders the friendly dead-end', async () => {
    mockFetch({});
    const w = await mountPage();

    expect(w.find('[data-testid="proposal-not-found"]').exists()).toBe(true);
    expect(w.find('[data-testid="accept-btn"]').exists()).toBe(false);
  });

  it('declined and expired states are terminal messages, no action buttons', async () => {
    mockFetch({
      'GET /api/proposals/tok-abc': {
        ...LINE_PAYLOAD,
        estimate: { ...LINE_PAYLOAD.estimate, status: 'declined', declined_at: '2026-08-13T01:00:00Z' },
      },
    });
    let w = await mountPage();
    expect(w.find('[data-testid="declined-msg"]').exists()).toBe(true);
    expect(w.find('[data-testid="open-actions"]').exists()).toBe(false);

    mockFetch({
      'GET /api/proposals/tok-abc': {
        ...LINE_PAYLOAD,
        estimate: { ...LINE_PAYLOAD.estimate, status: 'expired' },
      },
    });
    w = await mountPage();
    expect(w.find('[data-testid="expired-msg"]').exists()).toBe(true);
    expect(w.find('[data-testid="open-actions"]').exists()).toBe(false);
  });

  it('never toasts a list-shaped 422 detail at a customer', async () => {
    global.fetch = vi.fn((url, opts = {}) => {
      if ((opts.method || 'GET') === 'GET') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(LINE_PAYLOAD) });
      }
      return Promise.resolve({
        ok: false, status: 422,
        json: () => Promise.resolve({ detail: [{ loc: ['body', 'tier_id'], msg: 'field required' }] }),
      });
    });
    const w = await mountPage();
    await w.find('[data-testid="accept-btn"]').trigger('click');
    await w.find('[data-testid="accept-confirm"]').trigger('click');
    await flushPromises();

    const summary = toastAdd.mock.calls.at(-1)[0].summary;
    expect(typeof summary).toBe('string');
    expect(summary).not.toContain('[object');
    expect(summary).toContain('Something went wrong');
  });

  it('blocks accept on a line estimate whose totals are missing (no price = no contract)', async () => {
    mockFetch({ 'GET /api/proposals/tok-abc': { ...LINE_PAYLOAD, totals: undefined } });
    const w = await mountPage();

    expect(w.find('[data-testid="accept-btn"]').attributes('disabled')).toBeDefined();
    expect(w.find('[data-testid="totals-missing-msg"]').exists()).toBe(true);
  });

  it('decline posts {} when no reason typed, and the reason when there is one', async () => {
    let lastBody = null;
    mockFetch({
      'GET /api/proposals/tok-abc': LINE_PAYLOAD,
      'POST /api/proposals/tok-abc/decline': (opts) => {
        lastBody = JSON.parse(opts.body);
        return { ...LINE_PAYLOAD, estimate: { ...LINE_PAYLOAD.estimate, status: 'declined' } };
      },
    });
    let w = await mountPage();
    await w.find('[data-testid="decline-btn"]').trigger('click');
    await w.find('[data-testid="decline-confirm"]').trigger('click');
    await flushPromises();
    expect(lastBody).toEqual({});

    w = await mountPage();
    await w.find('[data-testid="decline-btn"]').trigger('click');
    await w.find('[data-testid="decline-reason"]').setValue('Too expensive');
    await w.find('[data-testid="decline-confirm"]').trigger('click');
    await flushPromises();
    expect(lastBody).toEqual({ reason: 'Too expensive' });
  });
});
