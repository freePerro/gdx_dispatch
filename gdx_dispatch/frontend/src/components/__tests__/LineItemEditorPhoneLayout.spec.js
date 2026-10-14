/**
 * LineItemEditor stacks its lines when its own box is narrower than its row
 * (GDXA-938).
 *
 * The one-row grid needs 492px with no optional columns and 940px with every
 * invoice column. Narrower than that, the row scrolled sideways inside the
 * editor and the Total -- the second-to-last track -- was the column
 * off-screen: on a 412px phone, a phone turned sideways, and a 1280px laptop.
 * The editor now measures its own content box (ResizeObserver) against the row
 * THIS editor renders and turns each line into a label/value card when the row
 * does not fit.
 *
 * jsdom has no layout, so the observer is stubbed here and fed widths. The
 * rendered proof is e2e/invoice-line-editor-stack.spec.js (412 to 1920px),
 * which CI does not run; this file is the part of the guard that does.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue([]), post: vi.fn() }),
}));

import LineItemEditor from '../LineItemEditor.vue';

const SRC = readFileSync(join(__dirname, '..', 'LineItemEditor.vue'), 'utf8');
const STYLE = SRC.slice(SRC.indexOf('<style'));

// Every invoice column on: category, cost, taxable + incl. install, margin.
const INVOICE_FLAGS = {
  categories: [{ label: 'Parts', value: 'Parts' }],
  showTaxable: true,
  showCost: true,
  showMargin: true,
};

let observers = [];
let frames = [];
class FakeResizeObserver {
  constructor(cb) { this.cb = cb; this.disconnected = false; observers.push(this); }
  observe(el) { this.el = el; }
  disconnect() { this.disconnected = true; }
  report(width) { this.cb([{ target: this.el, contentRect: { width } }]); }
}

function mountEditor(props = {}) {
  return mount(LineItemEditor, {
    props: { lines: [{ description: 'Door', quantity: 1, unit_price: 100 }], ...props },
    global: {
      directives: { tooltip: () => {} },
      stubs: { CatalogPickerDialog: true, LaborPickerDialog: true },
    },
  });
}

function flushFrames() {
  const due = frames;
  frames = [];
  for (const cb of due) if (cb) cb();
}

async function atWidth(w, width) {
  observers[observers.length - 1].report(width);
  flushFrames();
  await nextTick();
  return w.find('[data-testid="line-items-editor"]').classes().includes('lines-stacked');
}

describe('LineItemEditor — stacks when its row does not fit', () => {
  beforeEach(() => {
    observers = [];
    frames = [];
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    vi.stubGlobal('requestAnimationFrame', (cb) => frames.push(cb));
    vi.stubGlobal('cancelAnimationFrame', (id) => { frames[id - 1] = null; });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('stacks the full invoice row on a phone, a landscape phone and a 1280px laptop box', async () => {
    const w = mountEditor(INVOICE_FLAGS);
    // 288: 412px phone. 727: Pixel 8 landscape. 876: 1280px laptop.
    for (const width of [288, 727, 876, 939]) {
      expect(await atWidth(w, width), `${width}px`).toBe(true);
    }
  });

  it('keeps the full invoice row as a grid once it fits (1366px laptop box)', async () => {
    // 900px of tracks + 10 gaps of 4 = 940.
    const w = mountEditor(INVOICE_FLAGS);
    expect(await atWidth(w, 940)).toBe(false);
    expect(await atWidth(w, 962)).toBe(false);
  });

  it('keeps a bare editor on the grid in a 600px change-order dialog', async () => {
    // Bare row: 36+150+60+100+90+36 = 472 tracks + 5 gaps of 4 = 492.
    const w = mountEditor();
    expect(await atWidth(w, 540)).toBe(false);
    expect(await atWidth(w, 492)).toBe(false);
    expect(await atWidth(w, 491)).toBe(true);
    expect(await atWidth(w, 368)).toBe(true);
  });

  it('follows the width back and forth, and disconnects on unmount', async () => {
    const w = mountEditor(INVOICE_FLAGS);
    expect(await atWidth(w, 300)).toBe(true);
    expect(await atWidth(w, 1000)).toBe(false);
    w.unmount();
    expect(observers[0].disconnected).toBe(true);
  });

  it('restacks on the next frame, never inside the observer callback', async () => {
    // Restacking inside the callback resizes the box mid-step: the browser
    // raises "ResizeObserver loop completed with undelivered notifications"
    // and App.vue toasts it as "Something went wrong".
    const w = mountEditor(INVOICE_FLAGS);
    const editor = () => w.find('[data-testid="line-items-editor"]').classes();
    observers[0].report(300);
    await nextTick();
    expect(editor()).not.toContain('lines-stacked');
    flushFrames();
    await nextTick();
    expect(editor()).toContain('lines-stacked');
  });

  it('drops a pending frame on unmount', () => {
    const w = mountEditor(INVOICE_FLAGS);
    observers[0].report(300);
    w.unmount();
    expect(frames.every((f) => f === null)).toBe(true);
  });

  it('renders the desktop grid when no ResizeObserver exists', () => {
    vi.unstubAllGlobals();
    vi.stubGlobal('ResizeObserver', undefined);
    const w = mountEditor(INVOICE_FLAGS);
    expect(w.find('[data-testid="line-items-editor"]').classes()).not.toContain('lines-stacked');
  });

  it('renders one label per cell, Total included, in invoice mode', () => {
    const w = mountEditor(INVOICE_FLAGS);
    const labels = w.find('.line-item-row').findAll('.line-label').map((l) => l.text());
    expect(labels).toEqual([
      'Category', 'Description', 'Qty', 'Cost', 'Unit Price',
      'Taxable', 'Incl. install', 'Margin', 'Total',
    ]);
  });

  it('labels a locked row too, and keeps the Total on it', () => {
    const w = mountEditor({
      ...INVOICE_FLAGS,
      lines: [{ description: 'Deposit', quantity: 1, unit_price: -200, locked: true }],
      lockedPredicate: (l) => l.locked,
    });
    const row = w.find('.line-item-row-locked');
    expect(row.findAll('.line-label').map((l) => l.text())).toContain('Total');
    expect(w.find('[data-testid="line-total-0"]').text()).toBe('$-200.00');
  });
});

describe('LineItemEditor — the stylesheet the class drives', () => {
  it('reads the track list from a custom property, never an inline gridTemplateColumns', () => {
    // An inline declaration beats every stylesheet rule, so the stacked
    // layout could never restack the row.
    expect(SRC).not.toMatch(/gridTemplateColumns:\s*(cols|gridTracks)/);
    expect(SRC).toMatch(/'--line-tracks':\s*gridTracks\.value\.join/);
    expect(STYLE).toMatch(/\.line-item-row\s*\{[^}]*grid-template-columns:\s*var\(--line-tracks\)/);
  });

  it('keeps the field labels out of the desktop grid', () => {
    expect(STYLE).toMatch(/\n\.line-label\s*\{\s*display:\s*none/);
  });

  it('scopes every stacked-layout rule to .lines-stacked', () => {
    // A stacked rule that lost its scope would apply on the desktop grid too.
    expect(STYLE).toMatch(/\.lines-stacked \.line-item-header\s*\{\s*display:\s*none/);
    expect(STYLE).toMatch(/\.lines-stacked \.line-item-row\s*\{[^}]*grid-template-columns:\s*minmax\(/);
    expect(STYLE).toMatch(/\.lines-stacked \.line-label\s*\{[^}]*display:\s*block/);
    // Nothing outside .lines-stacked hides the header.
    const unscopedHeaderHide = STYLE.match(/(^|\n)\.line-item-header\s*\{\s*display:\s*none/);
    expect(unscopedHeaderHide).toBeNull();
  });
});
