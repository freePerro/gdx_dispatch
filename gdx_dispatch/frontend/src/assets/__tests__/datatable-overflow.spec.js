/**
 * GDXA-946 — lock the horizontal scroll every DataTable relies on at phone width.
 *
 * 124 call sites still pass `responsiveLayout="scroll"`, a PrimeVue 3 prop that
 * PrimeVue 4 dropped. They do not clip, because PrimeVue 4 puts an INLINE
 * `overflow: auto` on every table container, scrollable or not
 * (datatable/style inlineStyles.tableContainer, applied by sx() with when=true).
 * Measured at 412px on the invoice line table: container 330px wide, table
 * 715px, page scrollWidth 412 — it scrolls inside the card. No app CSS supplies
 * this, so a PrimeVue upgrade that stops doing it would clip every wide table
 * at once; this test is the tripwire.
 *
 * jsdom has no layout, so this pins the contract (the inline style), not the
 * painted scroll — that was walked in a browser (GDXA-946 report).
 */
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import { h } from 'vue';
import PrimeVue from 'primevue/config';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';

function containerStyle(props) {
  const w = mount(
    { render: () => h(DataTable, { value: [{ a: 1 }], ...props }, () => [h(Column, { field: 'a' })]) },
    { global: { plugins: [PrimeVue] } },
  );
  const el = w.find('[data-pc-section="tablecontainer"]');
  expect(el.exists()).toBe(true);
  const style = el.element.style.overflow;
  w.unmount();
  return style;
}

describe('DataTable horizontal overflow (GDXA-946)', () => {
  it('a DataTable without `scrollable` still gets overflow:auto on its container', () => {
    expect(containerStyle({})).toBe('auto');
  });

  it('a scrollable DataTable gets it too', () => {
    expect(containerStyle({ scrollable: true })).toBe('auto');
  });

  it('responsiveLayout is not a DataTable prop in this PrimeVue (it falls through as a dead attribute)', () => {
    expect(Object.keys(DataTable.props ?? {})).not.toContain('responsiveLayout');
  });
});
