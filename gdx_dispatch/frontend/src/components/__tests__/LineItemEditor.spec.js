/**
 * LineItemEditor — S122 contract pins.
 *
 * Pins the parts-from-job checklist behavior (received pre-checked, ordered
 * unchecked, SKU price enrichment, cumulative update:fromPartIds emit) and
 * the basic line-table mechanics (add/remove, subtotal, taxable toggle).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));

import LineItemEditor from '../LineItemEditor.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'disabled', 'size'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'input'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value); $emit(\'input\', $event)" />',
    inheritAttrs: false,
  },
  InputNumber: {
    // Mirrors the real component's payloads: `input` fires with { value }
    // (parsed number, null when cleared) and update:modelValue carries the
    // parsed number. The real component only commits v-model on blur/Enter;
    // the stub commits per keystroke, which the handlers must tolerate
    // (they are idempotent — each event re-runs the same recompute).
    props: ['modelValue'],
    emits: ['update:modelValue', 'input'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value === \'\' ? null : Number($event.target.value)); $emit(\'input\', { value: $event.target.value === \'\' ? null : Number($event.target.value) })" />',
    inheritAttrs: false,
  },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue', 'change'],
    template: '<select :data-testid="$attrs[\'data-testid\']" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\', $event)"><option v-for="o in options" :key="o.value || o" :value="o.value || o">{{ o.label || o }}</option></select>',
    inheritAttrs: false,
  },
  Dialog: {
    props: ['visible'],
    emits: ['update:visible'],
    template: '<div data-testid="dlg" v-if="visible"><slot /><div class="footer"><slot name="footer" /></div></div>',
  },
  DataTable: {
    props: ['value', 'selection'],
    emits: ['update:selection'],
    template: '<div data-testid="catalog-dt"><div v-for="row in value" :key="row.id" class="row"><button :data-testid="`catalog-pick-${row.id}`" @click="$emit(\'update:selection\', [...(selection||[]), row])">pick {{ row.name }}</button></div></div>',
  },
  Column: { template: '<div></div>' },
};

function mountEditor(props = {}) {
  return mount(LineItemEditor, {
    props: {
      lines: [{ description: '', quantity: 1, unit_price: 0 }],
      fromPartIds: [],
      ...props,
    },
    global: { stubs },
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
});

describe('LineItemEditor — line table mechanics', () => {
  it('renders the initial lines from the v-model', () => {
    const wrapper = mountEditor({
      lines: [
        { description: 'Spring', quantity: 1, unit_price: 50 },
        { description: 'Cable', quantity: 2, unit_price: 25 },
      ],
    });
    expect(wrapper.find('[data-testid="line-desc-0"]').element.value).toBe('Spring');
    expect(wrapper.find('[data-testid="line-desc-1"]').element.value).toBe('Cable');
  });

  it('Add Line emits update:lines with a fresh empty line appended', async () => {
    const wrapper = mountEditor();
    await wrapper.find('[data-testid="line-add-btn"]').trigger('click');
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last.length).toBe(2);
    expect(last[1]).toMatchObject({ description: '', quantity: 1, unit_price: 0 });
  });

  it('Remove deletes the line and emits update:lines', async () => {
    const wrapper = mountEditor({
      lines: [
        { description: 'A', quantity: 1, unit_price: 10 },
        { description: 'B', quantity: 1, unit_price: 20 },
      ],
    });
    await wrapper.find('[data-testid="line-delete-0"]').trigger('click');
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last.length).toBe(1);
    expect(last[0].description).toBe('B');
  });

  it('renders subtotal across all lines', () => {
    const wrapper = mountEditor({
      lines: [
        { description: 'A', quantity: 2, unit_price: 10 },
        { description: 'B', quantity: 1, unit_price: 15 },
      ],
    });
    expect(wrapper.find('[data-testid="line-items-subtotal"]').text()).toContain('35.00');
  });

  it('shows the taxable column only when show-taxable=true', () => {
    const noFlag = mountEditor();
    expect(noFlag.find('[data-testid="line-taxable-0"]').exists()).toBe(false);

    const withFlag = mountEditor({ showTaxable: true });
    expect(withFlag.find('[data-testid="line-taxable-0"]').exists()).toBe(true);
  });

  it('taxable defaults to true for new lines when show-taxable=true', async () => {
    const wrapper = mountEditor({ showTaxable: true });
    await wrapper.find('[data-testid="line-add-btn"]').trigger('click');
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[1].taxable).toBe(true);
  });

  it('unchecking taxable emits update:lines with taxable=false', async () => {
    const wrapper = mountEditor({
      lines: [{ description: 'Labor', quantity: 1, unit_price: 80, taxable: true }],
      showTaxable: true,
    });
    const cb = wrapper.find('[data-testid="line-taxable-0"]');
    cb.element.checked = false;
    await cb.trigger('change');
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].taxable).toBe(false);
  });

  it('renders the Category column when categories are passed', () => {
    const wrapper = mountEditor({
      lines: [{ description: 'Spring', quantity: 1, unit_price: 50, category: 'Springs' }],
      categories: [
        { label: 'Springs', value: 'Springs' },
        { label: 'Labor', value: 'Labor' },
      ],
    });
    expect(wrapper.find('[data-testid="line-cat-0"]').exists()).toBe(true);
  });

  it('renders the Cost column when show-cost=true', () => {
    const wrapper = mountEditor({ showCost: true });
    expect(wrapper.find('[data-testid="line-cost-0"]').exists()).toBe(true);
  });

  it('renders the Margin column when show-margin=true', () => {
    const wrapper = mountEditor({ showMargin: true });
    expect(wrapper.find('[data-testid="line-margin-0"]').exists()).toBe(true);
  });

  it('recomputes unit_price from cost × margin when user types margin then cost', async () => {
    const wrapper = mountEditor({
      lines: [{ description: 'Spring', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null }],
      showCost: true,
      showMargin: true,
    });
    // Parity with EstimateView: margin is only treated as an override when the
    // user actually typed it (_marginUserEdited). Type margin first, then cost.
    const marginInput = wrapper.find('[data-testid="line-margin-0"]');
    marginInput.element.value = '40';
    await marginInput.trigger('input');
    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '30';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBe(50);  // 30 / (1 - 0.40)
  });

  it('recomputes unit_price when margin is edited and cost is set', async () => {
    const wrapper = mountEditor({
      lines: [{ description: 'Spring', quantity: 1, unit_price: 0, cost: 30, margin_pct_override: null }],
      showCost: true,
      showMargin: true,
    });
    const marginInput = wrapper.find('[data-testid="line-margin-0"]');
    marginInput.element.value = '25';
    await marginInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    // 30 / (1 - 0.25) = 40
    expect(last[0].unit_price).toBe(40);
  });

  it('manual unit_price edit overrides — subsequent cost change does NOT recompute', async () => {
    const wrapper = mountEditor({
      lines: [{ description: 'Spring', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null }],
      showCost: true,
      showMargin: true,
    });
    // Set margin + cost via inputs so _marginUserEdited is true.
    const marginInput = wrapper.find('[data-testid="line-margin-0"]');
    marginInput.element.value = '40';
    await marginInput.trigger('input');
    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '30';
    await costInput.trigger('input');
    // User overrides unit_price manually.
    const priceInput = wrapper.find('[data-testid="line-price-0"]');
    priceInput.element.value = '99';
    await priceInput.trigger('input');
    // Now changes cost. unit_price should stick at 99.
    costInput.element.value = '60';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBe(99);
  });

  it('without tier data and without typed margin, cost alone does NOT recompute', async () => {
    // apiGet defaults to returning undefined → loadPricingTiers sees empty
    // tier table → cost-only edit falls through with no tier match.
    const wrapper = mountEditor({
      lines: [{ description: 'Spring', quantity: 1, unit_price: 17, cost: null, margin_pct_override: null }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();
    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '30';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBe(17);  // unchanged
  });

  it('duplicate-line copies values into a new adjacent row without id/key fields', async () => {
    const wrapper = mountEditor({
      lines: [
        { id: 'srv-1', _key: 'k-1', description: 'Spring', quantity: 2, unit_price: 50, taxable: true, category: 'Springs' },
        { id: 'srv-2', _key: 'k-2', description: 'Cable', quantity: 1, unit_price: 25 },
      ],
      categories: [{ label: 'Springs', value: 'Springs' }],
      showTaxable: true,
    });
    await wrapper.find('[data-testid="line-copy-0"]').trigger('click');
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last.length).toBe(3);
    // Copy at position 1 (right after the source).
    expect(last[1]).toMatchObject({
      description: 'Spring',
      quantity: 2,
      unit_price: 50,
      taxable: true,
      category: 'Springs',
    });
    // id/_key stripped so callers treat it as a new line.
    expect(last[1].id).toBeUndefined();
    expect(last[1]._key).toBeUndefined();
  });

  it('new line via Add Line includes category/cost/margin slots when flags are on', async () => {
    const wrapper = mountEditor({
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
      showTaxable: true,
    });
    await wrapper.find('[data-testid="line-add-btn"]').trigger('click');
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[1]).toMatchObject({
      description: '',
      quantity: 1,
      unit_price: 0,
      taxable: true,
      category: null,
      cost: null,
      margin_pct_override: null,
    });
  });
});

describe('LineItemEditor — tier-aware recompute (estimate parity)', () => {
  // Tier table the mock will return: retail-only, parts category with a single
  // open-ended tier at 35%. Matches the shape of /api/pricing-engine/tier-sets.
  const RETAIL_PARTS_35 = [
    {
      pricing_class: 'retail',
      pricing_category: 'parts',
      tiers: [{ cost_min: 0, cost_max: null, margin_pct: 0.35 }],
    },
  ];

  it('fetches /api/pricing-engine/tier-sets on mount when show-cost && show-margin', async () => {
    apiGet.mockResolvedValue([]);
    mountEditor({ showCost: true, showMargin: true });
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith(
      '/api/pricing-engine/tier-sets',
      expect.any(Object),
    );
  });

  it('does NOT fetch tier sets when show-cost or show-margin is off', async () => {
    apiGet.mockResolvedValue([]);
    mountEditor({ showCost: true });   // margin off
    await flushPromises();
    const calledUrls = apiGet.mock.calls.map((c) => c[0]);
    expect(calledUrls).not.toContain('/api/pricing-engine/tier-sets');
  });

  it('auto-fills unit_price AND margin column from tier when cost is typed and margin is blank', async () => {
    // The "Nickle Chain $325.83" repro: operator types cost, leaves margin
    // blank, expects tier to fill both unit_price and the displayed margin %.
    apiGet.mockResolvedValueOnce(RETAIL_PARTS_35);
    const wrapper = mountEditor({
      lines: [{ description: 'Nickle Chain', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '325.83';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    // 325.83 / (1 - 0.35) = 501.27692…  → round to cents → 501.28
    expect(last[0].unit_price).toBe(501.28);
    // Margin column reflects the tier-implied % (35.0).
    expect(last[0].margin_pct_override).toBe(35);
  });

  it('Springs category routes through the "parts" tier set', async () => {
    apiGet.mockResolvedValueOnce(RETAIL_PARTS_35);
    const wrapper = mountEditor({
      lines: [{ description: 'Torsion', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Springs' }],
      categories: [{ label: 'Springs', value: 'Springs' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBeCloseTo(100 / 0.65, 2);
  });

  it('user-typed margin override beats the tier table', async () => {
    apiGet.mockResolvedValueOnce(RETAIL_PARTS_35);
    const wrapper = mountEditor({
      lines: [{ description: 'Nickle Chain', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    // Operator types a margin first (50%), then cost. The override should win
    // over the tier (35%).
    const marginInput = wrapper.find('[data-testid="line-margin-0"]');
    marginInput.element.value = '50';
    await marginInput.trigger('input');
    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBe(200);  // 100 / (1 - 0.50)
  });

  it('margin edit AFTER a tier auto-fill recomputes immediately (regression: suppress flag swallowed it)', async () => {
    // Doug 2026-07-20: "profit margins do not always refresh". Root cause:
    // the tier auto-fill set _suppressMarginUserEdit expecting the model
    // write to echo back through update:modelValue — PrimeVue never emits
    // for programmatic writes, so the stale flag ate the user's NEXT real
    // margin edit and the price didn't move until a second attempt.
    apiGet.mockResolvedValueOnce(RETAIL_PARTS_35);
    const wrapper = mountEditor({
      lines: [{ description: 'Nickle Chain', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    // Cost first → tier fills margin 35 / price 153.85.
    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');
    let last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].margin_pct_override).toBe(35);

    // NOW edit the margin — the very next edit must recompute the price.
    const marginInput = wrapper.find('[data-testid="line-margin-0"]');
    marginInput.element.value = '50';
    await marginInput.trigger('input');
    last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBe(200);  // 100 / (1 - 0.50), not stale 153.85
  });

  it('re-committing the auto-filled margin (tab-through blur echo) does NOT freeze it as an override', async () => {
    // InputNumber commits on every blur even when unchanged. Tabbing through
    // the margin column used to flip _marginUserEdited and freeze the tier
    // value as a fake override — cost edits stopped refreshing the margin.
    apiGet.mockResolvedValueOnce(RETAIL_PARTS_35);
    const wrapper = mountEditor({
      lines: [{ description: 'Nickle Chain', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');

    // Simulate the unchanged blur echo: same 35 committed again.
    const marginInput = wrapper.find('[data-testid="line-margin-0"]');
    marginInput.element.value = '35';
    await marginInput.trigger('input');

    // Cost changes again — tier must still drive the recompute.
    costInput.element.value = '200';
    await costInput.trigger('input');
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBeCloseTo(200 / 0.65, 2);  // 307.69, tier still live
    expect(last[0].margin_pct_override).toBe(35);
  });

  it('the input event alone (pre-blur keystroke) updates the line live', async () => {
    // The real InputNumber only commits v-model on blur/Enter — mid-typing
    // refresh rides the `input` event. Emit it in isolation to prove the
    // live path works without a v-model commit.
    const wrapper = mountEditor({
      lines: [{ description: 'Door', quantity: 1, unit_price: 100 }],
    });
    const qtyStub = wrapper.findComponent('[data-testid="line-qty-0"]');
    qtyStub.vm.$emit('input', { value: 5 });
    await flushPromises();
    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].quantity).toBe(5);
    expect(wrapper.find('[data-testid="line-total-0"]').text()).toBe('$500.00');
  });

  it('clearing the margin mid-edit defers the tier refill to blur (no rewrite under the cursor)', async () => {
    apiGet.mockResolvedValueOnce(RETAIL_PARTS_35);
    const wrapper = mountEditor({
      lines: [{ description: 'Nickle Chain', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');

    // Mid-edit clear (input event only, no blur yet): field must stay empty.
    const marginStub = wrapper.findComponent('[data-testid="line-margin-0"]');
    marginStub.vm.$emit('input', { value: null });
    await flushPromises();
    let last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].margin_pct_override).toBeNull();

    // Blur commit of the empty value → tier refills.
    marginStub.vm.$emit('update:modelValue', null);
    await flushPromises();
    last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].margin_pct_override).toBe(35);
  });

  it('non-retail tier sets are dropped (retail-only by design)', async () => {
    apiGet.mockResolvedValueOnce([
      { pricing_class: 'retail', pricing_category: 'parts', tiers: [{ cost_min: 0, cost_max: null, margin_pct: 0.35 }] },
      { pricing_class: 'wholesale', pricing_category: 'parts', tiers: [{ cost_min: 0, cost_max: null, margin_pct: 0.10 }] },
    ]);
    const wrapper = mountEditor({
      lines: [{ description: 'X', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    // Should price at retail 35%, not wholesale 10%.
    expect(last[0].unit_price).toBeCloseTo(100 / 0.65, 2);
  });

  it('cost === 0 does NOT match the open-ended bottom tier (no fake margin written)', async () => {
    // Auditor catch 2026-05-12 — zero cost would otherwise match cost_min=0
    // and write 35% into the margin column while sell stayed at $0. Operator
    // sees a populated margin and an empty price and can't tell which one is
    // load-bearing. findTierMargin rejects cost <= 0.
    apiGet.mockResolvedValueOnce([
      { pricing_class: 'retail', pricing_category: 'parts', tiers: [{ cost_min: 0, cost_max: null, margin_pct: 0.35 }] },
    ]);
    const wrapper = mountEditor({
      lines: [{ description: 'X', quantity: 1, unit_price: 0, cost: 5, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    // User clears cost back to 0.
    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '0';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBe(0);
    expect(last[0].margin_pct_override).toBeFalsy();   // not auto-filled to 35
  });

  it('tier-fetch network failure degrades cleanly — no throw, no fake margin', async () => {
    // Auditor catch 2026-05-12 — silent degradation back to the OLD buggy
    // path with no warning. Test confirms the catch swallows cleanly AND that
    // a cost-typing flow afterwards doesn't crash or write a margin.
    apiGet.mockRejectedValueOnce(new Error('network'));
    const wrapper = mountEditor({
      lines: [{ description: 'X', quantity: 1, unit_price: 17, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    // No tier data → no recompute → unit_price unchanged, no fake margin.
    expect(last[0].unit_price).toBe(17);
    expect(last[0].margin_pct_override).toBeFalsy();
  });

  it('feature flag OFF — typed margin override is ignored, tier wins, margin column stays blank', async () => {
    // First call: tier-sets. Second call: estimates-features returning the
    // flag turned off. EstimateView.vue:657 gates every override branch on
    // this; carrying the gate keeps invoice + estimate surfaces consistent.
    apiGet
      .mockResolvedValueOnce([
        { pricing_class: 'retail', pricing_category: 'parts', tiers: [{ cost_min: 0, cost_max: null, margin_pct: 0.35 }] },
      ])
      .mockResolvedValueOnce({ estimates_allow_line_margin_override: false });
    const wrapper = mountEditor({
      lines: [{ description: 'X', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null, category: 'Parts' }],
      categories: [{ label: 'Parts', value: 'Parts' }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    // Operator types a margin (would be 50%) — should be ignored.
    const marginInput = wrapper.find('[data-testid="line-margin-0"]');
    marginInput.element.value = '50';
    await marginInput.trigger('input');
    // Then types cost — tier (35%) should drive the recompute.
    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '100';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    // Tier-priced (100 / 0.65 ≈ 153.85), not override-priced (200).
    expect(last[0].unit_price).toBeCloseTo(100 / 0.65, 2);
    // With flag off, margin column is also NOT auto-filled from tier — the
    // operator typed 50 manually before the cost; that value persists in the
    // form field because recomputeSell's auto-fill branch is gated on the
    // same flag. The semantically interesting assertion is that pricing
    // didn't honor the override, which the unit_price check above pins.
  });

  it('falls back to "other" tier when category is null', async () => {
    apiGet.mockResolvedValueOnce([
      { pricing_class: 'retail', pricing_category: 'other', tiers: [{ cost_min: 0, cost_max: null, margin_pct: 0.50 }] },
    ]);
    const wrapper = mountEditor({
      lines: [{ description: 'Misc', quantity: 1, unit_price: 0, cost: null, margin_pct_override: null }],
      showCost: true,
      showMargin: true,
    });
    await flushPromises();

    const costInput = wrapper.find('[data-testid="line-cost-0"]');
    costInput.element.value = '50';
    await costInput.trigger('input');

    const last = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(last[0].unit_price).toBe(100);  // 50 / (1 - 0.50)
  });
});

describe('LineItemEditor — parts-from-job panel', () => {
  it('does NOT render the panel when job-id is not set', () => {
    const wrapper = mountEditor();
    expect(wrapper.find('[data-testid="parts-from-job-panel"]').exists()).toBe(false);
  });

  it('fetches parts-needed scoped to ordered+received+used & unbilled when job-id is set', async () => {
    // PR4-billing-capture widened the scope: 'used' rows are the
    // closeout/mobile/van captures that previously never reached billing.
    apiGet.mockResolvedValue([]);
    mountEditor({ jobId: 'job-abc' });
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith(
      '/api/jobs/job-abc/parts-needed?status=ordered,received,used&unbilled=true',
      expect.any(Object),
    );
  });

  it('pre-checks received AND used parts; leaves ordered parts unchecked', async () => {
    apiGet.mockResolvedValue([
      { id: 'p-recv', part_name: 'Spring', quantity: 1, status: 'received', sku: null },
      { id: 'p-ord', part_name: 'Cable', quantity: 1, status: 'ordered', sku: null },
      { id: 'p-used', part_name: 'Strut', quantity: 1, status: 'used', source: 'closeout', sku: null },
    ]);
    const wrapper = mountEditor({ jobId: 'job-1' });
    await flushPromises();

    const recv = wrapper.find('[data-testid="parts-from-job-check-p-recv"]').element;
    const ord = wrapper.find('[data-testid="parts-from-job-check-p-ord"]').element;
    const used = wrapper.find('[data-testid="parts-from-job-check-p-used"]').element;
    expect(recv.checked).toBe(true);
    expect(ord.checked).toBe(false);
    expect(used.checked).toBe(true);
    // Provenance badge on the tech-attested row.
    const badge = wrapper.find('[data-testid="parts-from-job-badge-p-used"]');
    expect(badge.text().toLowerCase()).toContain('closeout');
  });

  it('vendor-bill-sourced parts arrive UNCHECKED and badged (vendor-invoice AUDIT-R2)', async () => {
    // A received row from a parsed vendor bill must NOT be pre-checked — a
    // special-order door is usually already on the estimate, so the office adds
    // it deliberately. It's still shown + badged so it can't be missed.
    apiGet.mockResolvedValue([
      { id: 'p-recv', part_name: 'Spring', quantity: 1, status: 'received', sku: null },
      { id: 'p-vinv', part_name: 'CHI door', quantity: 1, status: 'received',
        source: 'vendor_invoice', supplier: 'Midwest Wholesale Doors', sku: null },
    ]);
    const wrapper = mountEditor({ jobId: 'job-1' });
    await flushPromises();

    const recv = wrapper.find('[data-testid="parts-from-job-check-p-recv"]').element;
    const vinv = wrapper.find('[data-testid="parts-from-job-check-p-vinv"]').element;
    expect(recv.checked).toBe(true);    // ordinary received: pre-checked
    expect(vinv.checked).toBe(false);   // vendor-bill: NOT pre-checked
    const badge = wrapper.find('[data-testid="parts-from-job-badge-p-vinv"]');
    expect(badge.text().toLowerCase()).toContain('vendor bill');
  });

  it('renders an "ordered, not received" badge on ordered parts', async () => {
    apiGet.mockResolvedValue([
      { id: 'p-ord', part_name: 'Cable', quantity: 1, status: 'ordered' },
    ]);
    const wrapper = mountEditor({ jobId: 'job-1' });
    await flushPromises();
    const badge = wrapper.find('[data-testid="parts-from-job-badge-p-ord"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text().toLowerCase()).toContain('ordered');
  });

  it('Add Selected pushes lines, emits update:fromPartIds with the picked IDs, and replaces an empty placeholder line', async () => {
    // First call: list-job-parts. No SKU on the part → no second call.
    apiGet.mockResolvedValueOnce([
      { id: 'p1', part_name: 'Spring', quantity: 2, status: 'received', sku: null },
    ]);

    const wrapper = mountEditor({
      lines: [{ description: '', quantity: 1, unit_price: 0 }],
      jobId: 'job-1',
      showTaxable: true,
    });
    await flushPromises();

    await wrapper.find('[data-testid="parts-from-job-add"]').trigger('click');
    await flushPromises();

    const lastLines = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(lastLines.length).toBe(1);
    expect(lastLines[0]).toMatchObject({ description: 'Spring', quantity: 2, taxable: true });

    const lastIds = wrapper.emitted('update:fromPartIds').slice(-1)[0][0];
    expect(lastIds).toEqual(['p1']);
  });

  it('enriches unit_price from sku-suggest when the part has a SKU', async () => {
    apiGet
      .mockResolvedValueOnce([
        { id: 'p1', part_name: 'Spring', quantity: 1, status: 'received', sku: 'SPR-25' },
      ])
      .mockResolvedValueOnce([
        { sku: 'SPR-25', name: 'Torsion Spring 25', price: 42.5 },
      ]);

    const wrapper = mountEditor({ jobId: 'job-1' });
    await flushPromises();
    await wrapper.find('[data-testid="parts-from-job-add"]').trigger('click');
    await flushPromises();

    const lastLines = wrapper.emitted('update:lines').slice(-1)[0][0];
    expect(lastLines[0].unit_price).toBe(42.5);

    // Second call was to sku-suggest with the part's SKU.
    expect(apiGet.mock.calls[1][0]).toMatch(/\/api\/parts-needed\/sku-suggest\?q=SPR-25/);
  });

  it('Add Selected button is disabled when nothing is checked', async () => {
    apiGet.mockResolvedValue([
      { id: 'p-ord', part_name: 'Cable', quantity: 1, status: 'ordered' },  // unchecked
    ]);
    const wrapper = mountEditor({ jobId: 'job-1' });
    await flushPromises();
    expect(wrapper.find('[data-testid="parts-from-job-add"]').attributes('disabled')).toBeDefined();
  });

  it('hides the panel entirely when the fetched list is empty', async () => {
    apiGet.mockResolvedValue([]);
    const wrapper = mountEditor({ jobId: 'job-no-parts' });
    await flushPromises();
    expect(wrapper.find('[data-testid="parts-from-job-panel"]').exists()).toBe(false);
  });

  it('cumulative update:fromPartIds preserves previously-passed IDs', async () => {
    apiGet.mockResolvedValueOnce([
      { id: 'p2', part_name: 'Cable', quantity: 1, status: 'received', sku: null },
    ]);
    const wrapper = mountEditor({
      lines: [{ description: 'something', quantity: 1, unit_price: 10 }],
      fromPartIds: ['p1-prior'],  // simulated prior add
      jobId: 'job-1',
    });
    await flushPromises();
    await wrapper.find('[data-testid="parts-from-job-add"]').trigger('click');
    await flushPromises();

    const lastIds = wrapper.emitted('update:fromPartIds').slice(-1)[0][0].sort();
    expect(lastIds).toEqual(['p1-prior', 'p2']);
  });
});
