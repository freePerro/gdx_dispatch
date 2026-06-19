/**
 * PlatformRecovery — MH-0 regression suite.
 *
 * Locks behavior of the workspace picker shown when the host doesn't
 * resolve to a tenant (audit P0 #1):
 *   - slug input normalizes pasted hostnames / protocol-prefixed input
 *   - submit button enables/disables on a 2+ char normalized slug
 *   - target URL preview matches the redirect we'll perform
 *   - signup + marketing links are present and point at the right place
 *   - brand-blue submit (MH-2 contrast policy — no severity="success")
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import PlatformRecovery from '../PlatformRecovery.vue';

const RouterLinkStub = {
  props: ['to'],
  template: '<a :href="typeof to === \'string\' ? to : \'#\'" :data-to="to"><slot /></a>',
};

function mountRecovery() {
  return mount(PlatformRecovery, {
    global: { stubs: { 'router-link': RouterLinkStub } },
  });
}

describe('PlatformRecovery', () => {
  beforeEach(() => {
    // Each test sets up its own window.location.assign mock.
  });

  it('renders the picker copy + slug input + signup + marketing links', () => {
    const w = mountRecovery();
    expect(w.get('[data-testid="platform-recovery"]').exists()).toBe(true);
    expect(w.text()).toMatch(/Choose your workspace/i);
    expect(w.text()).toMatch(/This URL isn't tied to an active workspace/i);
    expect(w.find('[data-testid="recovery-slug"]').exists()).toBe(true);
    expect(w.find('[data-testid="recovery-submit"]').exists()).toBe(true);
    expect(w.find('[data-testid="recovery-signup"]').exists()).toBe(true);
    expect(w.find('[data-testid="recovery-marketing"]').exists()).toBe(true);
  });

  it('keeps submit disabled when input is empty or 1 char', async () => {
    const w = mountRecovery();
    const submit = w.get('[data-testid="recovery-submit"]');
    expect(submit.attributes('disabled')).toBeDefined();
    await w.get('[data-testid="recovery-slug"]').setValue('g');
    expect(submit.attributes('disabled')).toBeDefined();
  });

  it('enables submit at 2+ chars and shows target URL hint', async () => {
    const w = mountRecovery();
    await w.get('[data-testid="recovery-slug"]').setValue('gdx');
    expect(w.get('[data-testid="recovery-submit"]').attributes('disabled')).toBeUndefined();
    const hint = w.get('[data-testid="recovery-target"]');
    expect(hint.text()).toContain('https://gdx.example.com/login');
  });

  it('normalizes a pasted full hostname down to the slug', async () => {
    const w = mountRecovery();
    await w.get('[data-testid="recovery-slug"]').setValue('gdx.example.com');
    expect(w.get('[data-testid="recovery-target"]').text()).toContain(
      'https://gdx.example.com/login',
    );
  });

  it('normalizes a pasted protocol-prefixed URL', async () => {
    const w = mountRecovery();
    await w.get('[data-testid="recovery-slug"]').setValue('https://gdx.example.com/login');
    expect(w.get('[data-testid="recovery-target"]').text()).toContain(
      'https://gdx.example.com/login',
    );
  });

  it('strips slug characters outside [a-z0-9-]', async () => {
    const w = mountRecovery();
    await w.get('[data-testid="recovery-slug"]').setValue('Foo Bar!@# baz_99');
    // letters lowercased, spaces+specials+underscore stripped
    expect(w.get('[data-testid="recovery-target"]').text()).toContain(
      'https://foobarbaz99.example.com/login',
    );
  });

  it('redirects to the workspace login on submit', async () => {
    const w = mountRecovery();
    await w.get('[data-testid="recovery-slug"]').setValue('gdx');
    // jsdom's window.location.assign is non-configurable on the property
    // itself — replacing the whole `location` object works around it.
    const originalLocation = window.location;
    const assign = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...originalLocation, assign, href: originalLocation.href },
    });
    try {
      // Trigger the form submit (the button is type=submit inside .form,
      // but jsdom doesn't always propagate click→submit reliably).
      await w.get('form').trigger('submit.prevent');
      expect(assign).toHaveBeenCalledWith('https://gdx.example.com/login');
    } finally {
      Object.defineProperty(window, 'location', {
        configurable: true,
        writable: true,
        value: originalLocation,
      });
    }
  });

  it('signup link points to /signup', () => {
    const w = mountRecovery();
    const signup = w.get('[data-testid="recovery-signup"]');
    expect(signup.attributes('data-to')).toBe('/signup');
  });

  it('marketing link points to the public marketing site', () => {
    const w = mountRecovery();
    const m = w.get('[data-testid="recovery-marketing"]');
    expect(m.attributes('href')).toBe('https://example.com');
  });

  // MH-2 contrast policy guard: the submit button must use the brand-blue
  // var(--primary) (not PrimeVue severity="success" emerald which fails AA).
  it('submit button uses --primary, not emerald success token', () => {
    const w = mountRecovery();
    const submit = w.get('[data-testid="recovery-submit"]');
    // Vue scoped styles don't apply in jsdom, but the inline class
    // contract is what we lock — no `p-button-success`, has `submit-btn`.
    expect(submit.classes()).toContain('submit-btn');
    expect(submit.classes()).not.toContain('p-button-success');
  });
});
