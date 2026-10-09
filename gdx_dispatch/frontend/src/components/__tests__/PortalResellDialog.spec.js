/**
 * Customer portal — "Resell this" on an estimate (contractor resale quotes, PR B).
 *
 * Pinned:
 *  1. Opening it reads the profile: the markup starts at their default.
 *  2. Before the terms are agreed it sends them to My Branding instead of a form.
 *  3. Make my quote POSTs the fields to the estimate's resale route, then
 *     offers the PDF and My Quotes — no dead end.
 *  4. A refusal shows the server's reason and keeps the form.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PortalResellDialog from '../PortalResellDialog.vue';

const vModel = (tag = 'input') => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  inheritAttrs: false,
  template: `<${tag} :data-testid="$attrs['data-testid']" :value="modelValue ?? ''" @input="$emit('update:modelValue', $event.target.value)" />`,
});

const stubs = {
  Dialog: { props: ['visible'], template: '<div v-if="visible" data-testid="resell-dialog"><slot /></div>' },
  Button: {
    props: ['label', 'loading', 'type'],
    emits: ['click'],
    inheritAttrs: false,
    template: '<button :type="type || \'button\'" :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Message: { inheritAttrs: false, template: '<div :data-testid="$attrs[\'data-testid\']"><slot /></div>' },
  InputText: vModel(),
  Textarea: vModel('textarea'),
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    inheritAttrs: false,
    template: `<input :data-testid="$attrs['data-testid']" :value="modelValue ?? ''" @input="$emit('update:modelValue', $event.target.value === '' ? null : Number($event.target.value))" />`,
  },
};

const ESTIMATE = { id: 'est-1', estimate_number: 'EST-7' };
const ROW = { id: 'rq-1', reference: 'Q-0001', markup_pct: 30, base_subtotal: 1000, resale_subtotal: 1300, options: null };

function profile(accepted, extra = {}) {
  return { company_name: 'Acme Doors', default_markup_pct: 20, disclaimer: { accepted }, ...extra };
}

async function open(fetchImpl) {
  const fetcher = vi.fn(fetchImpl);
  const downloader = vi.fn(async () => {});
  const w = mount(PortalResellDialog, {
    props: { visible: false, estimate: ESTIMATE, fetcher, downloader },
    global: { stubs },
  });
  await w.setProps({ visible: true });
  await flushPromises();
  return { w, fetcher, downloader };
}

describe('PortalResellDialog', () => {
  it('starts the markup at the account default', async () => {
    const { w } = await open(async () => profile(true));
    expect(w.get('[data-testid="resell-markup"]').element.value).toBe('20');
  });

  it('before the terms are agreed it offers My Branding, not a form', async () => {
    const { w } = await open(async () => profile(false));
    expect(w.find('[data-testid="resell-submit-btn"]').exists()).toBe(false);
    await w.get('[data-testid="resell-goto-branding"]').trigger('click');
    expect(w.emitted('goto-branding')).toHaveLength(1);
  });

  it('posts the quote, then offers the PDF and My Quotes', async () => {
    const { w, fetcher, downloader } = await open(async (url, opts) => (opts?.method === 'POST' ? ROW : profile(true)));
    await w.get('[data-testid="resell-markup"]').setValue('30');
    await w.get('[data-testid="resell-customer-name"]').setValue('Pat Example');
    await w.get('form').trigger('submit');
    await flushPromises();
    const [url, opts] = fetcher.mock.calls.find(([, o]) => o?.method === 'POST');
    expect(url).toBe('/portal/estimates/est-1/resale');
    expect(JSON.parse(opts.body)).toEqual({
      markup_pct: 30, reference: '', end_customer_name: 'Pat Example', end_customer_address: '', notes: '',
    });
    expect(w.emitted('created')[0][0]).toEqual(ROW);
    expect(w.get('[data-testid="resell-done"]').text()).toContain('Q-0001');
    expect(w.get('[data-testid="resell-done"]').text()).toContain('your price $1,300.00, our price before tax $1,000.00');
    await w.get('[data-testid="resell-download-btn"]').trigger('click');
    await flushPromises();
    expect(downloader).toHaveBeenCalledWith('/portal/resale-quotes/rq-1/pdf', 'quote-Q-0001.pdf');
    await w.get('[data-testid="resell-goto-quotes"]').trigger('click');
    expect(w.emitted('goto-quotes')).toHaveLength(1);
  });

  it('a cleared markup blocks the quote rather than selling at our price', async () => {
    const { w, fetcher } = await open(async (url, opts) => (opts?.method === 'POST' ? ROW : profile(true)));
    await w.get('[data-testid="resell-markup"]').setValue('');
    expect(w.find('[data-testid="resell-markup-required"]').exists()).toBe(true);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(fetcher.mock.calls.some(([, o]) => o?.method === 'POST')).toBe(false);
    await w.get('[data-testid="resell-markup"]').setValue('0');
    await w.get('form').trigger('submit');
    await flushPromises();
    const [, opts] = fetcher.mock.calls.find(([, o]) => o?.method === 'POST');
    expect(JSON.parse(opts.body).markup_pct).toBe(0);
  });

  it('a refusal shows the reason and keeps the form', async () => {
    const { w } = await open(async (url, opts) => {
      if (opts?.method === 'POST') throw Object.assign(new Error('Not found'), { status: 404 });
      return profile(true);
    });
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.get('[data-testid="resell-error"]').text()).toBe('Not found');
    expect(w.find('[data-testid="resell-submit-btn"]').exists()).toBe(true);
    expect(w.emitted('created')).toBeUndefined();
  });
});
