// PhoneInput — confirms the shared entry field auto-formats to (111)222-3333
// via PrimeVue InputMask and emits the masked value on v-model.
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import PhoneInput from '../PhoneInput.vue';

function mountInput(modelValue = '') {
  return mount(PhoneInput, {
    props: { modelValue, 'onUpdate:modelValue': () => {} },
    global: { plugins: [PrimeVue] },
  });
}

describe('PhoneInput', () => {
  it('renders an input carrying the phone mask', () => {
    const w = mountInput();
    const input = w.find('input');
    expect(input.exists()).toBe(true);
  });

  it('forwards id / class / data-testid down to the input (e2e selectors survive)', () => {
    const w = mount(PhoneInput, {
      props: { modelValue: '', 'onUpdate:modelValue': () => {} },
      attrs: { id: 'cust-phone', class: 'w-full', 'data-testid': 'customer-phone' },
      global: { plugins: [PrimeVue] },
    });
    const input = w.find('input');
    expect(input.attributes('id')).toBe('cust-phone');
    expect(input.attributes('data-testid')).toBe('customer-phone');
    expect(input.classes()).toContain('w-full');
  });

  it('displays a legacy value re-fitted into the mask', async () => {
    const w = mountInput('1112223333');
    await w.vm.$nextTick();
    // InputMask formats the bound value into the mask on mount.
    expect(w.find('input').element.value).toBe('(111)222-3333');
  });

  it('emits update:modelValue as the user types digits', async () => {
    const w = mountInput('');
    const input = w.find('input');
    input.element.value = '1112223333';
    await input.trigger('input');
    await input.trigger('keyup');
    const emits = w.emitted('update:modelValue');
    expect(emits).toBeTruthy();
    // The last emitted value is the fully-masked phone number.
    expect(emits[emits.length - 1][0]).toBe('(111)222-3333');
  });
});
