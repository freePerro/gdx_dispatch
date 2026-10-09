/**
 * Customer portal — "My Branding" for contractor and wholesale accounts
 * (contractor resale quotes, PR B).
 *
 * Pinned:
 *  1. Before the terms are agreed, the form is locked and "I agree" PUTs the
 *     version shown, and nothing else.
 *  2. A 409 (the wording changed) reloads the new wording.
 *  3. Save PUTs every field, so a cleared field is cleared on the server.
 *  4. A logo goes up as multipart `file`, then the preview is re-read through
 *     the authed blob fetcher.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PortalBrandingTab from '../PortalBrandingTab.vue';

const vModel = (tag = 'input') => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  inheritAttrs: false,
  template: `<${tag} :data-testid="$attrs['data-testid']" :value="modelValue ?? ''" @input="$emit('update:modelValue', $event.target.value)" />`,
});

const stubs = {
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    inheritAttrs: false,
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { template: '<div><slot name="title" /><slot name="subtitle" /><slot name="content" /></div>' },
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

function profile(overrides = {}) {
  return {
    company_name: null, phone: null, email: null, address: null, website: null, license_no: null,
    terms_text: null, default_markup_pct: 0, has_logo: false,
    disclaimer: { text: 'Private to your account.', version: 'v1', accepted: false, accepted_at: null },
    ...overrides,
  };
}
const ACCEPTED = { text: 'Private to your account.', version: 'v1', accepted: true, accepted_at: '2026-10-08T12:00:00+00:00' };

function setup(fetchImpl) {
  const fetcher = vi.fn(fetchImpl);
  const blobFetcher = vi.fn(async () => new Blob(['png'], { type: 'image/png' }));
  const w = mount(PortalBrandingTab, { props: { fetcher, blobFetcher }, global: { stubs } });
  return { w, fetcher, blobFetcher };
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:logo');
  globalThis.URL.revokeObjectURL = vi.fn();
});

describe('PortalBrandingTab', () => {
  it('locks the form until the terms are agreed, and I agree sends only the version', async () => {
    const { w, fetcher } = setup(async (url, opts) => {
      if (opts?.method === 'PUT') return profile({ disclaimer: ACCEPTED });
      return profile();
    });
    await flushPromises();
    expect(w.get('[data-testid="branding-disclaimer-text"]').text()).toBe('Private to your account.');
    expect(w.find('[data-testid="branding-locked-msg"]').exists()).toBe(true);
    expect(w.get('[data-testid="branding-save-btn"]').attributes('disabled')).toBeDefined();

    await w.get('[data-testid="branding-accept-btn"]').trigger('click');
    await flushPromises();
    const put = fetcher.mock.calls.find(([, o]) => o?.method === 'PUT');
    expect(JSON.parse(put[1].body)).toEqual({ accept_disclaimer_version: 'v1' });
    expect(w.find('[data-testid="branding-locked-msg"]').exists()).toBe(false);
    expect(w.find('[data-testid="branding-disclaimer-accepted"]').exists()).toBe(true);
    expect(w.get('[data-testid="branding-save-btn"]').attributes('disabled')).toBeUndefined();
    expect(w.emitted('changed')).toHaveLength(1);
  });

  it('a 409 shows the server message and reloads the new wording', async () => {
    let gets = 0;
    const { w } = setup(async (url, opts) => {
      if (opts?.method === 'PUT') throw Object.assign(new Error('The reseller terms changed. Reload and read them again.'), { status: 409 });
      gets += 1;
      return gets === 1 ? profile() : profile({ disclaimer: { ...profile().disclaimer, text: 'New words.', version: 'v2' } });
    });
    await flushPromises();
    await w.get('[data-testid="branding-accept-btn"]').trigger('click');
    await flushPromises();
    expect(gets).toBe(2);
    expect(w.get('[data-testid="branding-disclaimer-text"]').text()).toBe('New words.');
    expect(w.get('[data-testid="branding-accept-error"]').text()).toContain('terms changed');
  });

  it('save sends every field, so clearing one clears it', async () => {
    const { w, fetcher } = setup(async (url, opts) => {
      if (opts?.method === 'PUT') return profile({ ...JSON.parse(opts.body), disclaimer: ACCEPTED });
      return profile({ company_name: 'Acme Doors', phone: '555-0100', default_markup_pct: 25, disclaimer: ACCEPTED });
    });
    await flushPromises();
    expect(w.get('[data-testid="branding-company-name"]').element.value).toBe('Acme Doors');
    await w.get('[data-testid="branding-phone"]').setValue('');
    await w.get('[data-testid="branding-markup"]').setValue('30');
    await w.get('[data-testid="branding-save-btn"]').trigger('click');
    await flushPromises();
    const body = JSON.parse(fetcher.mock.calls.find(([, o]) => o?.method === 'PUT')[1].body);
    expect(body).toEqual({
      company_name: 'Acme Doors', phone: '', email: '', address: '', website: '', license_no: '', terms_text: '',
      default_markup_pct: 30,
    });
    expect(w.find('[data-testid="branding-saved"]').exists()).toBe(true);
  });

  it('a cleared default markup blocks save rather than saving 0%', async () => {
    const { w, fetcher } = setup(async (url, opts) => {
      if (opts?.method === 'PUT') return profile({ ...JSON.parse(opts.body), disclaimer: ACCEPTED });
      return profile({ default_markup_pct: 25, disclaimer: ACCEPTED });
    });
    await flushPromises();
    await w.get('[data-testid="branding-markup"]').setValue('');
    expect(w.find('[data-testid="branding-markup-required"]').exists()).toBe(true);
    expect(w.get('[data-testid="branding-save-btn"]').attributes('disabled')).toBeDefined();
    await w.get('[data-testid="branding-save-btn"]').trigger('click');
    await flushPromises();
    expect(fetcher.mock.calls.some(([, o]) => o?.method === 'PUT')).toBe(false);
  });

  it('uploads a logo as multipart file and shows it from the authed blob', async () => {
    const { w, fetcher, blobFetcher } = setup(async () => profile({ disclaimer: ACCEPTED }));
    await flushPromises();
    expect(blobFetcher).not.toHaveBeenCalled();
    const input = w.get('[data-testid="branding-logo-input"]');
    const file = new File(['x'], 'logo.png', { type: 'image/png' });
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true });
    await input.trigger('change');
    await flushPromises();
    const [url, opts] = fetcher.mock.calls.find(([u]) => u === '/portal/reseller/logo');
    expect(url).toBe('/portal/reseller/logo');
    expect(opts.method).toBe('POST');
    expect(opts.body.get('file')).toBe(file);
    expect(blobFetcher).toHaveBeenCalledWith('/portal/reseller/logo');
    expect(w.get('[data-testid="branding-logo-img"]').attributes('src')).toBe('blob:logo');
  });

  it('a refused logo shows the server reason', async () => {
    const { w } = setup(async (url, opts) => {
      if (opts?.method === 'POST') throw Object.assign(new Error('Use a PNG, JPEG or WebP image'), { status: 415 });
      return profile({ disclaimer: ACCEPTED });
    });
    await flushPromises();
    const input = w.get('[data-testid="branding-logo-input"]');
    Object.defineProperty(input.element, 'files', { value: [new File(['x'], 'a.svg')], configurable: true });
    await input.trigger('change');
    await flushPromises();
    expect(w.get('[data-testid="branding-logo-error"]').text()).toBe('Use a PNG, JPEG or WebP image');
  });
});
