/**
 * Customer portal — "My Quotes" (contractor resale quotes, PR B).
 *
 * Pinned:
 *  1. Before the terms are agreed (403) it says where to go, not "could not load".
 *  2. Each quote shows our price before tax, their price and the markup; an
 *     options quote lists each option.
 *  3. Download PDF hands the path and a filename to the downloader.
 *  4. Delete happens only once confirmed, and drops the card.
 *  5. A bumped refreshKey re-reads the list.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PortalMyQuotesTab from '../PortalMyQuotesTab.vue';

let confirmAnswer = true;
const confirmDestructive = vi.fn((opts) => { if (confirmAnswer) opts.accept(); });
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive, confirmAsync: vi.fn() }),
}));

const stubs = {
  ConfirmDialog: true,
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    inheritAttrs: false,
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { inheritAttrs: false, template: '<div :data-testid="$attrs[\'data-testid\']"><slot name="title" /><slot name="subtitle" /><slot name="content" /></div>' },
  Message: { inheritAttrs: false, template: '<div :data-testid="$attrs[\'data-testid\']"><slot /></div>' },
};

const ROW = {
  id: 'rq-1', estimate_id: 'est-1', estimate_number: 'EST-7', reference: 'Q-0001',
  end_customer_name: 'Pat Example', markup_pct: 25, base_subtotal: 1000, resale_subtotal: 1250,
  markup_amount: 250, hide_line_prices: false, created_at: '2026-10-08T12:00:00+00:00', options: null,
};
const OPTIONS_ROW = {
  ...ROW, id: 'rq-2', reference: 'Q-0002', base_subtotal: null, resale_subtotal: null, markup_amount: null,
  options: [{ name: 'Good', base_price: 1000, price: 1100, markup_amount: 100 }, { name: 'Best', base_price: 2000, price: 2200, markup_amount: 200 }],
};

function setup(fetchImpl, props = {}) {
  const fetcher = vi.fn(fetchImpl);
  const downloader = vi.fn(async () => {});
  const w = mount(PortalMyQuotesTab, { props: { fetcher, downloader, ...props }, global: { stubs } });
  return { w, fetcher, downloader };
}

beforeEach(() => { confirmAnswer = true; confirmDestructive.mockClear(); });

describe('PortalMyQuotesTab', () => {
  it('before the terms are agreed it points at My Branding', async () => {
    const { w } = setup(async () => { throw Object.assign(new Error('Agree to the reseller terms on My Branding first'), { status: 403 }); });
    await flushPromises();
    expect(w.find('[data-testid="my-quotes-locked"]').exists()).toBe(true);
    expect(w.find('[data-testid="my-quotes-error"]').exists()).toBe(false);
    await w.get('[data-testid="my-quotes-goto-branding"]').trigger('click');
    expect(w.emitted('goto-branding')).toHaveLength(1);
  });

  it('an empty list says how to make a quote and links to estimates', async () => {
    const { w } = setup(async () => []);
    await flushPromises();
    expect(w.get('[data-testid="my-quotes-empty"]').text()).toContain('Resell this');
    await w.get('[data-testid="my-quotes-goto-estimates"]').trigger('click');
    expect(w.emitted('goto-estimates')).toHaveLength(1);
  });

  it('shows our price, theirs and the markup, and lists options', async () => {
    const { w } = setup(async () => [ROW, OPTIONS_ROW]);
    await flushPromises();
    expect(w.get('[data-testid="my-quote-base-rq-1"]').text()).toBe('$1,000.00');
    expect(w.get('[data-testid="my-quote-price-rq-1"]').text()).toBe('$1,250.00');
    expect(w.get('[data-testid="my-quote-markup-rq-1"]').text()).toBe('25% · $250.00');
    expect(w.get('[data-testid="my-quote-rq-1"]').text()).toContain('For Pat Example');
    expect(w.get('[data-testid="my-quote-rq-1"]').text()).toContain('EST-7');
    const opts = w.get('[data-testid="my-quote-options-rq-2"]').text();
    expect(opts).toContain('Good');
    expect(opts).toContain('$2,200.00');
    expect(w.find('[data-testid="my-quote-base-rq-2"]').exists()).toBe(false);
  });

  it('Download PDF passes the quote path and its filename', async () => {
    const { w, downloader } = setup(async () => [ROW]);
    await flushPromises();
    await w.get('[data-testid="my-quote-pdf-rq-1"]').trigger('click');
    await flushPromises();
    expect(downloader).toHaveBeenCalledWith('/portal/resale-quotes/rq-1/pdf', 'quote-Q-0001.pdf');
  });

  it('deletes only once confirmed, then drops the card', async () => {
    const { w, fetcher } = setup(async (url, opts) => (opts?.method === 'DELETE' ? { id: 'rq-1', deleted: true } : [ROW]));
    await flushPromises();
    confirmAnswer = false;
    await w.get('[data-testid="my-quote-delete-rq-1"]').trigger('click');
    await flushPromises();
    expect(fetcher.mock.calls.some(([, o]) => o?.method === 'DELETE')).toBe(false);
    confirmAnswer = true;
    await w.get('[data-testid="my-quote-delete-rq-1"]').trigger('click');
    await flushPromises();
    expect(fetcher).toHaveBeenCalledWith('/portal/resale-quotes/rq-1', { method: 'DELETE' });
    expect(w.find('[data-testid="my-quote-rq-1"]').exists()).toBe(false);
  });

  it('a failed delete keeps the card and says so', async () => {
    const { w } = setup(async (url, opts) => {
      if (opts?.method === 'DELETE') throw Object.assign(new Error('request failed: 500'), { status: 500 });
      return [ROW];
    });
    await flushPromises();
    await w.get('[data-testid="my-quote-delete-rq-1"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="my-quote-rq-1"]').exists()).toBe(true);
    expect(w.get('[data-testid="my-quotes-action-error"]').text()).toBe('Could not delete that quote.');
  });

  it("one card's download finishing does not re-enable another card's delete", async () => {
    let finishPdf;
    let finishDelete;
    const ROW2 = { ...ROW, id: 'rq-9', reference: 'Q-0009' };
    const { w, downloader } = setup(async (url, opts) => {
      if (opts?.method === 'DELETE') return new Promise((r) => { finishDelete = r; });
      return [ROW, ROW2];
    });
    downloader.mockImplementation(() => new Promise((r) => { finishPdf = r; }));
    await flushPromises();
    await w.get('[data-testid="my-quote-pdf-rq-1"]').trigger('click');
    await w.get('[data-testid="my-quote-delete-rq-9"]').trigger('click');
    await flushPromises();
    expect(w.get('[data-testid="my-quote-delete-rq-9"]').element.disabled).toBe(true);
    finishPdf();
    await flushPromises();
    expect(w.get('[data-testid="my-quote-delete-rq-9"]').element.disabled).toBe(true);
    finishDelete({ id: 'rq-9', deleted: true });
    await flushPromises();
    expect(w.find('[data-testid="my-quote-rq-9"]').exists()).toBe(false);
  });

  it('while a card is deleting, its own Download is off too', async () => {
    let finishDelete;
    const { w } = setup(async (url, opts) => {
      if (opts?.method === 'DELETE') return new Promise((r) => { finishDelete = r; });
      return [ROW];
    });
    await flushPromises();
    await w.get('[data-testid="my-quote-delete-rq-1"]').trigger('click');
    await flushPromises();
    expect(w.get('[data-testid="my-quote-pdf-rq-1"]').element.disabled).toBe(true);
    expect(w.get('[data-testid="my-quote-delete-rq-1"]').element.disabled).toBe(true);
    finishDelete({ id: 'rq-1', deleted: true });
    await flushPromises();
    expect(w.find('[data-testid="my-quote-rq-1"]').exists()).toBe(false);
  });

  it('re-reads the list when refreshKey changes', async () => {
    let rows = [];
    const { w, fetcher } = setup(async () => rows);
    await flushPromises();
    rows = [ROW];
    await w.setProps({ refreshKey: 1 });
    await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(w.find('[data-testid="my-quote-rq-1"]').exists()).toBe(true);
  });
});
