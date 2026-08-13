/**
 * PaymentCaptureForm — the single cash/check capture form (2026-08-13).
 *
 * Mount-tested rather than source-pinned because the risky logic is behavioural,
 * not structural: whether the confirmation actually gates the payload, and what
 * the payload contains. Both are money decisions.
 *
 * Pins:
 *  1. Prefills balanceDue — the three hand-rolled forms it replaces opened at 0
 *     and made the operator retype a number the app already knew. A mistyped LOW
 *     amount is silent damage (the shortfall gets credit-memo'd as "superseded"
 *     and the customer is over-billed while every screen reads paid).
 *  2. Cash is gated behind a confirmation; rejecting it emits NOTHING.
 *  3. Check is NOT gated — the check # goes to `reference`, which migration
 *     056's partial unique index dedupes server-side. Cash has no such key,
 *     which is the whole reason for the prompt.
 *  4. The date is the tenant-zone day, never a UTC slice.
 *  5. A blank reference is null, not "" — the dedupe index is partial on
 *     `reference IS NOT NULL`, so empty strings would defeat it.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const confirmAsync = vi.fn();

vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync, confirmDestructive: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({
    tenantTimezone: { value: 'America/Chicago' },
    ensureLoaded: vi.fn(),
    // A fixed date that is deliberately NOT today: if the component regressed
    // to `new Date().toISOString().slice(0,10)` it would return the real UTC
    // day and this assertion would fail. A stub returning today's date would
    // let that exact bug pass its own test.
    zonedDateKey: () => '2019-07-04',
  }),
}));

import PaymentCaptureForm from '../PaymentCaptureForm.vue';

function mountForm(props = {}) {
  return mount(PaymentCaptureForm, {
    props: { balanceDue: 500, ...props },
    global: {
      stubs: {
        // Stub the PrimeVue widgets down to plain inputs so the assertions are
        // about this component's logic, not PrimeVue's rendering.
        InputNumber: {
          name: 'InputNumber',
          props: ['modelValue', 'max'],
          emits: ['update:modelValue'],
          template: '<input class="amount" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
        },
        InputText: {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
        },
        Select: {
          props: ['modelValue', 'options'],
          emits: ['update:modelValue'],
          template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="o in options" :key="o" :value="o">{{ o }}</option></select>',
        },
        SelectButton: {
          props: ['modelValue', 'options'],
          emits: ['update:modelValue'],
          template: '<div class="selbtn"><button v-for="o in options" :key="o" type="button" @click="$emit(\'update:modelValue\', o)">{{ o }}</button></div>',
        },
        Button: {
          name: 'Button',
          props: ['label', 'disabled'],
          emits: ['click'],
          template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
        },
      },
    },
  });
}

beforeEach(() => {
  confirmAsync.mockReset();
  confirmAsync.mockResolvedValue(true);
});

describe('PaymentCaptureForm', () => {
  it('prefills the amount from balanceDue instead of 0', async () => {
    const w = mountForm({ balanceDue: 500 });
    const payload = await w.vm.collect();
    expect(payload.amount).toBe(500);
  });

  it('re-prefills when the payable amount changes', async () => {
    const w = mountForm({ balanceDue: 500 });
    await w.setProps({ balanceDue: 750 });
    const payload = await w.vm.collect();
    expect(payload.amount).toBe(750);
  });

  it('caps the amount input at the balance owed', () => {
    const w = mountForm({ balanceDue: 500 });
    expect(w.findComponent({ name: 'InputNumber' }).props('max')).toBe(500);
  });

  it('does not cap when capAtBalance is false', () => {
    const w = mountForm({ balanceDue: 500, capAtBalance: false });
    expect(w.findComponent({ name: 'InputNumber' }).props('max')).toBeNull();
  });

  it('asks for confirmation before returning a CASH payload', async () => {
    const w = mountForm({ balanceDue: 500, methods: ['Cash', 'Check'] });
    const payload = await w.vm.collect();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(confirmAsync.mock.calls[0][0].message).toMatch(/\$500\.00/);
    expect(payload).not.toBeNull();
  });

  it('returns NULL when the cash confirmation is rejected', async () => {
    confirmAsync.mockResolvedValue(false);
    const w = mountForm({ balanceDue: 500, methods: ['Cash', 'Check'] });
    expect(await w.vm.collect()).toBeNull();
  });

  it('emits nothing when the cash confirmation is rejected in standalone mode', async () => {
    confirmAsync.mockResolvedValue(false);
    const w = mountForm({ balanceDue: 500, methods: ['Cash', 'Check'], standalone: true });
    await w.find('button').trigger('click');
    await flushPromises();
    expect(w.emitted('submit')).toBeUndefined();
  });

  it('does NOT confirm a CHECK that carries a check number', async () => {
    // The check # lands in `reference`, which migration 056's partial unique
    // index dedupes server-side — that IS the duplicate protection, so no
    // human step is needed.
    const w = mountForm({ balanceDue: 500, methods: ['Check', 'Cash'] });
    await w.findAll('input').at(-1).setValue('1042');
    const payload = await w.vm.collect();
    expect(confirmAsync).not.toHaveBeenCalled();
    expect(payload).toMatchObject({ method: 'Check', reference: '1042' });
  });

  it('DOES confirm a check with the number left blank', async () => {
    // The rule is about dedupe keys, not method names. A reference-less Check
    // has no unique index behind it, exactly like cash — and an earlier version
    // of this component keyed the prompt on method alone, so this slipped
    // through with no prompt and no index.
    const w = mountForm({ balanceDue: 500, methods: ['Check', 'Cash'] });
    const payload = await w.vm.collect();
    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(payload.reference).toBeNull();
  });

  it('stamps the tenant-zone day, not a UTC slice', async () => {
    const w = mountForm({ balanceDue: 500 });
    const payload = await w.vm.collect();
    expect(payload.date).toBe('2019-07-04');
  });

  it('sends a blank reference as null so the partial unique index still applies', async () => {
    const w = mountForm({ balanceDue: 500, methods: ['Check'] });
    const payload = await w.vm.collect();
    expect(payload.reference).toBeNull();
  });

  it('trims a supplied check number', async () => {
    const w = mountForm({ balanceDue: 500, methods: ['Check'] });
    const refInput = w.findAll('input').at(-1);
    await refInput.setValue('  1042  ');
    const payload = await w.vm.collect();
    expect(payload.reference).toBe('1042');
  });

  it('refuses to produce a payload with no amount', async () => {
    const w = mountForm({ balanceDue: 0 });
    expect(await w.vm.collect()).toBeNull();
    expect(confirmAsync).not.toHaveBeenCalled();
  });

  it('emits the payload on submit in standalone mode', async () => {
    const w = mountForm({ balanceDue: 500, methods: ['Check'], standalone: true });
    await w.find('button').trigger('click');
    await flushPromises();
    const emitted = w.emitted('submit');
    expect(emitted).toHaveLength(1);
    expect(emitted[0][0]).toMatchObject({ amount: 500, method: 'Check', date: '2019-07-04' });
  });
});
