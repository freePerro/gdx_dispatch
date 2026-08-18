import { describe, it, expect, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import EmailBodyFrame from '../EmailBodyFrame.vue';

function mountFrame(props = {}) {
  return mount(EmailBodyFrame, { props });
}

describe('EmailBodyFrame', () => {
  it('renders the body inside a fully-sandboxed iframe (no allow-scripts)', () => {
    const w = mountFrame({ html: '<b>hi</b>', contentType: 'html' });
    const iframe = w.find('iframe');
    expect(iframe.exists()).toBe(true);
    // sandbox="" = every restriction on; scripts cannot run.
    expect(iframe.attributes('sandbox')).toBe('');
    expect(iframe.attributes('srcdoc')).toContain('<b>hi</b>');
  });

  it('NEVER injects body html into the parent document (no v-html)', () => {
    const w = mountFrame({ html: '<img src=x onerror=alert(1)>', contentType: 'html' });
    // The hostile markup must live ONLY inside the iframe's srcdoc string, not
    // as real DOM in the parent — so no <img> element is ever created here.
    expect(w.find('img').exists()).toBe(false);
    // And the parent's only child is the frame wrapper (+ iframe), never the
    // raw payload as elements.
    expect(w.findAll('*').some((el) => el.element.tagName === 'IMG')).toBe(false);
    expect(w.find('iframe').attributes('srcdoc')).toContain('onerror');
  });

  it('blocks remote images by default via CSP img-src data:', () => {
    const w = mountFrame({ html: '<img src="https://tracker/x.gif">', contentType: 'html' });
    const doc = w.find('iframe').attributes('srcdoc');
    expect(doc).toContain('Content-Security-Policy');
    expect(doc).toContain('img-src data:');
    expect(doc).not.toContain('img-src data: https: http:');
  });

  it('relaxes img-src only after the user opts into remote images', async () => {
    const w = mountFrame({ html: '<img src="https://tracker/x.gif">', contentType: 'html' });
    await w.find('[data-test="ebf-images-toggle"]').trigger('click');
    const doc = w.find('iframe').attributes('srcdoc');
    expect(doc).toContain('img-src data: https: http:');
  });

  it('CSP has no script-src at all (default-src none kills scripts)', () => {
    const w = mountFrame({ html: '<script>alert(1)</script>', contentType: 'html' });
    const doc = w.find('iframe').attributes('srcdoc');
    expect(doc).toContain("default-src 'none'");
    expect(doc).not.toContain('script-src');
  });

  it('escapes plain-text bodies instead of rendering them as html', () => {
    const w = mountFrame({ html: '<b>not bold</b>', contentType: 'text' });
    const doc = w.find('iframe').attributes('srcdoc');
    expect(doc).toContain('&lt;b&gt;not bold&lt;/b&gt;');
    expect(doc).toContain('<pre class="txt">');
  });

  it('does not offer the images toggle for plain-text bodies', () => {
    const w = mountFrame({ html: 'plain', contentType: 'text' });
    expect(w.find('[data-test="ebf-images-toggle"]').exists()).toBe(false);
  });

  it('shows a note when the live body could not be fetched', () => {
    const w = mountFrame({ html: 'preview', contentType: 'text', note: 'Showing preview — reconnect.' });
    expect(w.find('[data-test="ebf-note"]').text()).toContain('reconnect');
  });

  it('shows a loading state and no iframe while loading', () => {
    const w = mountFrame({ loading: true });
    expect(w.text()).toContain('Loading');
    expect(w.find('iframe').exists()).toBe(false);
  });
});

describe('EmailBodyFrame — white canvas in both themes (prod walks 2026-07-28 + 2026-08-18)', () => {
  // Two prod walks, two opposite failures, one lesson:
  // - 2026-07-28: media-query-only styling left dark slate text on a dark
  //   pane (fix attempt: inject light text + color:inherit in dark mode).
  // - 2026-08-18: that injection washed out to pale-grey-on-white whenever
  //   the sender's own white background filled the frame (Outlook bodies
  //   bring their own white wrappers) — quoted reply headers near invisible.
  // Resolution: NEVER theme sender-authored HTML. The email renders on an
  // explicit white canvas with dark default text in BOTH app themes, same
  // as the composer preview and Email Log detail.
  afterEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });

  it('renders a white canvas with dark text regardless of app theme', async () => {
    for (const theme of ['dark', 'light']) {
      document.documentElement.setAttribute('data-theme', theme);
      const w = mount(EmailBodyFrame, { props: { html: '<p>hello</p>', contentType: 'html' } });
      await flushPromises();
      const doc = w.find('iframe').attributes('srcdoc');
      expect(doc, theme).toContain('background:#ffffff');
      expect(doc, theme).toContain('color:#1e293b');
      expect(doc, theme).not.toContain('color:#e2e8f0');
      expect(doc, theme).not.toContain('color:inherit');
    }
  });

  it('srcdoc is theme-independent (no repaint machinery needed)', async () => {
    document.documentElement.setAttribute('data-theme', 'dark');
    const dark = mount(EmailBodyFrame, { props: { html: '<p>x</p>' } });
    document.documentElement.setAttribute('data-theme', 'light');
    const light = mount(EmailBodyFrame, { props: { html: '<p>x</p>' } });
    await flushPromises();
    expect(dark.find('iframe').attributes('srcdoc'))
      .toEqual(light.find('iframe').attributes('srcdoc'));
  });
});
