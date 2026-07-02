import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import FormField from '../FormField.vue';

function mountField(props = {}) {
  return mount(FormField, {
    props,
    global: { plugins: [PrimeVue] },
  });
}

describe('FormField', () => {
  it('renders label and associates it to the input', () => {
    const w = mountField({ label: 'Email', modelValue: '' });
    const label = w.find('label');
    const input = w.find('input');
    expect(label.exists()).toBe(true);
    expect(input.exists()).toBe(true);
    expect(label.attributes('for')).toBe(input.attributes('id'));
  });

  it('shows required asterisk when required=true', () => {
    const w = mountField({ label: 'Name', required: true, modelValue: '' });
    expect(w.find('.required-asterisk').exists()).toBe(true);
  });

  it('does not show asterisk by default', () => {
    const w = mountField({ label: 'Name', modelValue: '' });
    expect(w.find('.required-asterisk').exists()).toBe(false);
  });

  it('renders error message and sets aria-invalid + aria-describedby', () => {
    const w = mountField({ label: 'Email', error: 'Required', modelValue: '' });
    const err = w.find('.p-error');
    const input = w.find('input');
    expect(err.exists()).toBe(true);
    expect(err.text()).toBe('Required');
    expect(input.attributes('aria-invalid')).toBe('true');
    expect(input.attributes('aria-describedby')).toBe(err.attributes('id'));
  });

  it('renders hint when no error', () => {
    const w = mountField({ label: 'Email', hint: 'We never share', modelValue: '' });
    expect(w.find('.form-hint').text()).toBe('We never share');
  });

  it('emits update:modelValue on input change', async () => {
    const w = mountField({ label: 'Email', modelValue: '' });
    await w.find('input').setValue('foo@bar.com');
    expect(w.emitted('update:modelValue')[0]).toEqual(['foo@bar.com']);
  });

  it('renders textarea when as=textarea', () => {
    const w = mountField({ label: 'Notes', as: 'textarea', modelValue: '' });
    expect(w.find('textarea').exists()).toBe(true);
  });

  it('forwards fall-through attrs (data-testid, inputmode) to the inner input', () => {
    const w = mount(FormField, {
      props: { modelValue: '', label: 'Phone' },
      attrs: { 'data-testid': 'cust-phone', inputmode: 'tel' },
    });
    const input = w.find('input');
    expect(input.attributes('data-testid')).toBe('cust-phone');
    expect(input.attributes('inputmode')).toBe('tel');
    // The wrapper div must NOT swallow them.
    expect(w.find('.form-field').attributes('data-testid')).toBeUndefined();
  });
});
