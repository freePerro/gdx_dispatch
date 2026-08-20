/**
 * LaborPickerDialog — the two labor lanes, and the invariant between them.
 *
 * Doug 2026-08-19: Add Labor can bill "either" the matrix flat price or the
 * tech's attested hours. The rule that keeps that honest:
 *
 *   billed labor comes from ATTESTED hours only; code may not invent hours.
 *
 * A matrix row is a QUOTED FLAT PRICE, not a claim about duration. So the
 * matrix lane must never put an hours count in a description — that would be
 * the code asserting a number nobody attested, onto a customer-facing bill.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
}));

import LaborPickerDialog from '../LaborPickerDialog.vue';

const MATRIX = [
  {
    id: 'lpi-1', description: '16x7 Sectional Install', service_type: 'install',
    sku: null, width_ft: 16, height_ft: 7, flat_price: 650, assumed_man_hours: 6.5,
    active: true,
  },
  {
    id: 'lpi-2', description: '9x7 Sectional Install', service_type: 'install',
    sku: null, width_ft: 9, height_ft: 7, flat_price: 475, assumed_man_hours: 4.75,
    active: true,
  },
];

const CLOSEOUT = {
  has_closeout: true,
  closeout: { hours_worked: 9, techs_on_site: 2, closed_at: '2026-08-19T14:00:00Z' },
  labor_line: {
    description: 'Labor — 18.0 attested man-hours',
    quantity: 1,
    unit_price: 1620,
    // The server now says WHICH lane produced this. Service jobs => attested.
    source: 'attested',
    labor_price_item_id: null,
    man_hours: 9,
  },
};

const stubs = {
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible"><slot /><slot name="footer" /></div>',
  },
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  DataTable: {
    props: ['value', 'selection'],
    emits: ['update:selection'],
    template: `<div data-testid="labor-table">
      <button v-for="r in value" :key="r.id" :data-testid="'pick-' + r.id"
        @click="$emit('update:selection', [r])">{{ r.description }}</button>
    </div>`,
  },
  Column: { template: '<span><slot /></span>' },
};

function mountPicker(props = {}) {
  return mount(LaborPickerDialog, {
    props: { visible: true, ...props },
    global: { stubs },
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockImplementation((url) => {
    if (String(url).includes('labor-pricing/items')) return Promise.resolve(MATRIX);
    if (String(url).includes('pricing-engine/settings')) {
      return Promise.resolve({ loaded_labor_cost_per_hour: 65 });
    }
    if (String(url).includes('tax/config')) return Promise.resolve({ tax_labor: false });
    return Promise.resolve([]);
  });
});

describe('LaborPickerDialog — the matrix lane never invents hours', () => {
  it('emits a flat-price line with NO hours in the description', async () => {
    const w = mountPicker();
    await flushPromises();
    await w.find('[data-testid="pick-lpi-1"]').trigger('click');
    await w.find('[data-testid="labor-add-matrix"]').trigger('click');

    const [lines] = w.emitted('add')[0];
    expect(lines).toHaveLength(1);
    // THE invariant. 6.5 is the matrix's assumption, not attested time.
    expect(lines[0].description).toBe('16x7 Sectional Install');
    expect(lines[0].description).not.toMatch(/\d+(\.\d+)?\s*(h|hr|hour|man-hour)/i);
  });

  it('carries the matrix row id, hours snapshot and labor_source', async () => {
    const w = mountPicker();
    await flushPromises();
    await w.find('[data-testid="pick-lpi-1"]').trigger('click');
    await w.find('[data-testid="labor-add-matrix"]').trigger('click');

    expect(w.emitted('add')[0][0][0]).toMatchObject({
      unit_price: 650,
      category: 'Labor',
      labor_price_item_id: 'lpi-1',
      estimated_man_hours: 6.5,
      labor_source: 'matrix',
      _priceOverridden: true,
    });
  });

  it('snapshots cost from the loaded rate x assumed hours', async () => {
    // 65/h x 6.5h = 422.50 — used for the margin panel, never as an hours claim.
    const w = mountPicker();
    await flushPromises();
    await w.find('[data-testid="pick-lpi-1"]').trigger('click');
    await w.find('[data-testid="labor-add-matrix"]').trigger('click');
    expect(w.emitted('add')[0][0][0].cost).toBeCloseTo(422.5, 2);
  });
});

describe('LaborPickerDialog — the attested lane', () => {
  it('is hidden when the job has no closeout', async () => {
    const w = mountPicker({ closeout: null });
    await flushPromises();
    expect(w.find('[data-testid="labor-lane-attested"]').exists()).toBe(false);
  });

  it('bills the server-computed attested line and marks it attested', async () => {
    const w = mountPicker({ closeout: CLOSEOUT });
    await flushPromises();
    await w.find('[data-testid="labor-add-attested"]').trigger('click');

    expect(w.emitted('add')[0][0][0]).toMatchObject({
      description: 'Labor — 18.0 attested man-hours',
      unit_price: 1620,
      category: 'Labor',
      labor_source: 'attested',
      estimated_man_hours: 9,
    });
  });

  it('never sends a matrix row id on an attested line', async () => {
    const w = mountPicker({ closeout: CLOSEOUT });
    await flushPromises();
    await w.find('[data-testid="labor-add-attested"]').trigger('click');
    expect(w.emitted('add')[0][0][0].labor_price_item_id).toBeUndefined();
  });
});

describe('LaborPickerDialog — showing the disagreement instead of hiding it', () => {
  it('warns when the matrix assumption and the attested hours differ', async () => {
    // Matrix assumes 6.5h for a 16x7; the tech attested 9h. Hiding the attested
    // number behind a flat price is how the evidence gets lost.
    const w = mountPicker({ closeout: CLOSEOUT });
    await flushPromises();
    await w.find('[data-testid="pick-lpi-1"]').trigger('click');
    await flushPromises();

    const warn = w.find('[data-testid="labor-hours-disagreement"]');
    expect(warn.exists()).toBe(true);
    expect(warn.text()).toContain('6.5');
    expect(warn.text()).toContain('9.0');
  });

  it('stays silent when they agree closely enough to be the same story', async () => {
    // `man_hours` on the server's labor_line is authoritative — it is the
    // number that actually priced the attested line — so the fixture has to
    // move that, not just the raw closeout field.
    const close = {
      ...CLOSEOUT,
      closeout: { ...CLOSEOUT.closeout, hours_worked: 6.6 },
      labor_line: { ...CLOSEOUT.labor_line, man_hours: 6.6 },
    };
    const w = mountPicker({ closeout: close });
    await flushPromises();
    await w.find('[data-testid="pick-lpi-1"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="labor-hours-disagreement"]').exists()).toBe(false);
  });

  it('says so when the role cannot read the matrix, instead of showing nothing', async () => {
    apiGet.mockImplementation((url) => {
      if (String(url).includes('labor-pricing/items')) {
        return Promise.reject(Object.assign(new Error('nope'), { status: 403 }));
      }
      if (String(url).includes('tax/config')) return Promise.resolve({ tax_labor: false });
      return Promise.resolve({ loaded_labor_cost_per_hour: 65 });
    });
    const w = mountPicker();
    await flushPromises();
    expect(w.find('[data-testid="labor-forbidden"]').exists()).toBe(true);
    expect(w.find('[data-testid="labor-empty"]').exists()).toBe(false);
  });
});

describe('LaborPickerDialog — an install quote is never relabelled as attested', () => {
  // The blocker the p2 audit caught. /closeout-billing-suggestion returns
  // `labor_line` from EITHER lane: a service job yields attested hours, an
  // INSTALL job yields a quoted flat price from a matrix row. Treating both as
  // attested recorded a contract price as hours evidence, with hours that did
  // not price it — inverting the one invariant this dialog exists to hold.
  const INSTALL_CLOSEOUT = {
    has_closeout: true,
    closeout: { hours_worked: 9, techs_on_site: 2, closed_at: '2026-08-19T14:00:00Z' },
    labor_line: {
      description: '16x7 Sectional Install',
      quantity: 1,
      unit_price: 650,
      source: 'matrix',
      labor_price_item_id: 'lpi-1',
      man_hours: null,
    },
  };

  it('does NOT offer a matrix-sourced suggestion under the attested lane', async () => {
    const w = mountPicker({ closeout: INSTALL_CLOSEOUT });
    await flushPromises();
    expect(w.find('[data-testid="labor-lane-attested"]').exists()).toBe(false);
    expect(w.find('[data-testid="labor-lane-suggested-matrix"]').exists()).toBe(true);
  });

  it('bills it as matrix, naming the row that quoted it', async () => {
    const w = mountPicker({ closeout: INSTALL_CLOSEOUT });
    await flushPromises();
    await w.find('[data-testid="labor-add-suggested-matrix"]').trigger('click');

    const line = w.emitted('add')[0][0][0];
    expect(line.labor_source).toBe('matrix');
    expect(line.labor_price_item_id).toBe('lpi-1');
    // No hours claim: the closeout's 9h did not price this flat quote.
    expect(line.estimated_man_hours).toBeUndefined();
    expect(line.description).not.toMatch(/\d+(\.\d+)?\s*(h|hr|hour|man-hour)/i);
  });

  it('falls back to manual when the matrix row id is missing', async () => {
    // A quote we cannot attribute is "manual", never an unverifiable "matrix"
    // claim — the contract rejects matrix-without-an-id for the same reason.
    const noId = {
      ...INSTALL_CLOSEOUT,
      labor_line: { ...INSTALL_CLOSEOUT.labor_line, labor_price_item_id: null },
    };
    const w = mountPicker({ closeout: noId });
    await flushPromises();
    await w.find('[data-testid="labor-add-suggested-matrix"]').trigger('click');

    const line = w.emitted('add')[0][0][0];
    expect(line.labor_source).toBe('manual');
    expect(line.labor_price_item_id).toBeUndefined();
  });

  it('follows the tenant tax_labor setting rather than hardcoding it', async () => {
    // OFF (this tenant, MN construction contract) => labor is not taxed.
    const w = mountPicker({ closeout: CLOSEOUT });
    await flushPromises();
    await w.find('[data-testid="labor-add-attested"]').trigger('click');
    expect(w.emitted('add')[0][0][0].taxable).toBe(false);
  });

  it('taxes labor when the tenant has turned that on', async () => {
    // The closeout AUTODRAFT honours this flag. A picker that hardcoded false
    // would bill the same job differently depending on which route created the
    // line — the drift this repo has already paid for once.
    apiGet.mockImplementation((url) => {
      if (String(url).includes('labor-pricing/items')) return Promise.resolve(MATRIX);
      if (String(url).includes('tax/config')) return Promise.resolve({ tax_labor: true });
      return Promise.resolve({ loaded_labor_cost_per_hour: 65 });
    });
    const w = mountPicker({ closeout: CLOSEOUT });
    await flushPromises();
    await w.find('[data-testid="labor-add-attested"]').trigger('click');
    expect(w.emitted('add')[0][0][0].taxable).toBe(true);
  });

  it('defaults to NOT taxing labor when the config cannot be read', async () => {
    apiGet.mockImplementation((url) => {
      if (String(url).includes('labor-pricing/items')) return Promise.resolve(MATRIX);
      if (String(url).includes('tax/config')) return Promise.reject(new Error('down'));
      return Promise.resolve({ loaded_labor_cost_per_hour: 65 });
    });
    const w = mountPicker({ closeout: CLOSEOUT });
    await flushPromises();
    await w.find('[data-testid="labor-add-attested"]').trigger('click');
    // Defaulting true would re-introduce an overbill.
    expect(w.emitted('add')[0][0][0].taxable).toBe(false);
  });
});
