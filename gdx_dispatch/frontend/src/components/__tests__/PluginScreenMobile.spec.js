/**
 * PluginScreen on a phone (2026-08-11).
 *
 * PluginScreen is the generic renderer for every plugin's declared UI, and it
 * shipped with no responsive handling at all: a manifest-declared DataTable, a
 * 46rem detail dialog and 12rem-wide settings rows, none of which fit a 390px
 * viewport. Two halves are pinned here:
 *
 *   1. the JS decision — a `browser` screen streams a full-size remote page the
 *      operator drives by hand. CSS can't fix that, and shrinking it to 30%
 *      would still open a WebSocket and a server-side browser to render
 *      something unreadable. On a phone we say so instead of connecting.
 *   2. the CSS — jsdom applies no media queries, so the layout rules are pinned
 *      as source assertions (same approach as AppBottomNav.spec.js). If someone
 *      drops the breakpoint block, this fails rather than silently restoring
 *      the horizontal-overflow bug.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const BROWSER_MANIFEST = {
  screens: [
    {
      type: 'browser',
      title: 'HubX Workspace',
      url: 'https://example.invalid/',
      capture_endpoint: '/api/plugins/example/capture',
    },
  ],
};

const apiMock = vi.hoisted(() => ({ get: null, put: null, post: null }));
apiMock.get = vi.fn(async (url) => (url.endsWith('/ui') ? BROWSER_MANIFEST : []));
apiMock.put = vi.fn(async () => ({}));
apiMock.post = vi.fn(async () => ({}));

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => apiMock,
}));

// A real ref — the component destructures it and the template relies on Vue's
// ref unwrapping, which a plain object would not get.
const viewport = vi.hoisted(() => ({ ref: null }));
vi.mock('../../composables/useViewMode', async () => {
  const { ref } = await import('vue');
  viewport.ref = ref(false);
  return { useViewMode: () => ({ isMobileViewport: viewport.ref }) };
});

// eslint-disable-next-line import/first
import PluginScreen from '../PluginScreen.vue';

const stubs = {
  DataTable: true,
  Column: true,
  InputText: true,
  Button: true,
  Checkbox: true,
  Select: true,
  // The tab STRIP is stubbed; Tabs/TabPanels/TabPanel stay real so the panel
  // under test actually renders. PrimeVue's TabList schedules an ink-bar
  // setTimeout(150ms) on mount and never clears it (primevue/tablist mounted()),
  // so a fast spec tears jsdom down before it fires and vitest reports an
  // unhandled "HTMLElement is not defined". Nothing here needs the strip.
  TabList: true,
  Tab: true,
  BrowserStream: { template: '<div data-testid="browser-stream-stub" />' },
};

function mountScreen() {
  return mount(PluginScreen, { props: { pluginKey: 'example' }, global: { stubs } });
}

describe('PluginScreen — browser screen on a phone', () => {
  beforeEach(() => {
    viewport.ref.value = false;
  });

  it('streams the remote browser on a desktop viewport', async () => {
    const wrapper = mountScreen();
    await flushPromises();
    expect(wrapper.find('[data-testid="browser-stream-stub"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="plugin-screen-desktop-only"]').exists()).toBe(false);
  });

  it('explains itself instead of connecting on a phone viewport', async () => {
    viewport.ref.value = true;
    const wrapper = mountScreen();
    await flushPromises();

    // The socket is never opened — the stub would be present if it mounted.
    expect(wrapper.find('[data-testid="browser-stream-stub"]').exists()).toBe(false);

    const notice = wrapper.find('[data-testid="plugin-screen-desktop-only"]');
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain('needs a computer');
    // Says the rest of the plugin still works — a dead-end message would send
    // the tech looking for a laptop they don't need for the other tabs.
    expect(notice.text()).toContain('other tabs');
  });
});

/**
 * The riskiest edit in this change: PluginScreen now supplies `<Column>`'s
 * #body slot instead of letting PrimeVue render the field. CHI pricing's
 * Captured Quotes table is live in production and read on a desktop every day,
 * so this pins that the real DataTable still shows the real values — the rest
 * of the suite stubs Column away and would not notice it rendering blank.
 */
describe('PluginScreen — list cells with the real DataTable', () => {
  const LIST_MANIFEST = {
    screens: [{
      type: 'list',
      title: 'Captured Quotes',
      endpoint: '/api/plugins/example/quotes',
      columns: [
        { field: 'qcd', label: 'Quote #' },
        { field: 'price', label: 'Price' },
        { field: 'door.width', label: 'Width' },
      ],
    }],
  };
  const ROWS = [{ id: 1, qcd: 'Q-1042', price: '1,299.00', door: { width: 96 } }];

  async function mountList() {
    apiMock.get.mockImplementation(async (url) => (url.endsWith('/ui') ? LIST_MANIFEST : ROWS));
    const wrapper = mount(PluginScreen, {
      props: { pluginKey: 'example' },
      // DataTable + Column deliberately REAL here.
      global: { stubs: { InputText: true, Button: true, Checkbox: true, Select: true, TabList: true, Tab: true } },
    });
    await flushPromises();
    return wrapper;
  }

  it('renders every declared column value, including a dotted path', async () => {
    const wrapper = await mountList();
    const text = wrapper.text();
    expect(text).toContain('Q-1042');
    expect(text).toContain('1,299.00');
    expect(text).toContain('96');
  });

  it('carries a per-cell label for the phone card layout', async () => {
    const wrapper = await mountList();
    const labels = wrapper.findAll('.plugin-screen__cell-label').map((n) => n.text());
    // One per column, taken from the manifest — not a hardcoded nth-child list.
    expect(labels).toEqual(['Quote #', 'Price', 'Width']);
  });
});

const SRC = readFileSync(join(__dirname, '..', 'PluginScreen.vue'), 'utf8');

describe('PluginScreen — phone layout rules', () => {
  it('ships a mobile breakpoint block', () => {
    expect(SRC).toMatch(/@media\s*\(max-width:\s*768px\)/);
  });

  it('stacks table rows into cards (hides the header, frees the cell width)', () => {
    expect(SRC).toMatch(/\.p-datatable-thead\)?\s*\{\s*display:\s*none/);
    expect(SRC).toMatch(/\.p-datatable-tbody\s*>\s*tr\)\s*\{[^}]*flex-direction:\s*column/);
  });

  it('labels each cell, since the header row is gone in card mode', () => {
    // Hidden by default, revealed inside the breakpoint block.
    expect(SRC).toMatch(/\.plugin-screen__cell-label\s*\{\s*display:\s*none/);
    const mobileBlock = SRC.slice(SRC.search(/@media\s*\(max-width:\s*768px\)/));
    expect(mobileBlock).toMatch(/\.plugin-screen__cell-label\s*\{[^}]*display:\s*inline/);
  });

  it('lets the detail dialog shrink instead of overflowing at 46rem', () => {
    expect(SRC).toMatch(/:breakpoints="\{\s*'768px':\s*'95vw'\s*\}"/);
  });

  it('unpins the fixed-width settings and form controls at the breakpoint', () => {
    const mobileBlock = SRC.slice(SRC.search(/@media\s*\(max-width:\s*768px\)/));
    expect(mobileBlock).toMatch(/\.plugin-screen__ordered-name\s*\{[^}]*min-width:\s*0/);
    expect(mobileBlock).toMatch(/\.plugin-screen__create/);
  });
});
