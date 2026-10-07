// The quantity field's keyboard hint, against the REAL PrimeVue InputNumber.
// LineItemEditor.spec.js stubs InputNumber, so it cannot see this attribute.
//
// PrimeVue hints inputmode="numeric" unless minFractionDigits is set, and a
// phone's numeric keypad has no decimal point: on an invoice a tech could not
// type 2.5 hours. Change-order lines stay whole-number, so their hint stays
// numeric.
import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue([]), post: vi.fn() }),
}));

import LineItemEditor from '../LineItemEditor.vue';

function qtyInput(props) {
  const w = mount(LineItemEditor, {
    props: { lines: [{ description: 'Labor', quantity: 2.5, unit_price: 100 }], fromPartIds: [], ...props },
    global: { plugins: [PrimeVue], stubs: { CatalogPickerDialog: true, LaborPickerDialog: true } },
  });
  return w.find('[data-testid="line-qty-0"] input');
}

describe('LineItemEditor quantity keypad', () => {
  it('an invoice line (fractional) asks the phone for a keypad with a decimal point', () => {
    expect(qtyInput({ fractionalQuantity: true }).attributes('inputmode')).toBe('decimal');
  });

  it('a whole-number line keeps the numeric keypad', () => {
    expect(qtyInput({}).attributes('inputmode')).toBe('numeric');
  });
});
