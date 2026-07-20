import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
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
