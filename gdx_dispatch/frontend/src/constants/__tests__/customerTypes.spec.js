import { describe, expect, it } from 'vitest';
import {
  CUSTOMER_TYPES,
  CUSTOMER_TYPE_OPTIONS,
  customerTypeOptionsFor,
  normalizeCustomerType,
} from '../customerTypes';

describe('customerTypes (GDXA-413)', () => {
  it('options mirror the type list', () => {
    expect(CUSTOMER_TYPE_OPTIONS.map((o) => o.value)).toEqual(CUSTOMER_TYPES);
    expect(CUSTOMER_TYPES).toEqual(expect.arrayContaining(['Contractor', 'Wholesale']));
  });

  it('normalizes stored spellings to the canonical label', () => {
    expect(normalizeCustomerType('contractor')).toBe('Contractor');
    expect(normalizeCustomerType(' WHOLESALE ')).toBe('Wholesale');
    expect(normalizeCustomerType('property_manager')).toBe('Property Manager');
    expect(normalizeCustomerType('Property Manager')).toBe('Property Manager');
  });

  it('keeps an unknown value verbatim and uses the fallback only for empty', () => {
    expect(normalizeCustomerType('Builder')).toBe('Builder');
    expect(normalizeCustomerType(null)).toBe('Residential');
    expect(normalizeCustomerType('', '—')).toBe('—');
  });

  it('adds an unknown current value to the options so the Select is never blank', () => {
    expect(customerTypeOptionsFor('Contractor')).toBe(CUSTOMER_TYPE_OPTIONS);
    expect(customerTypeOptionsFor(null)).toBe(CUSTOMER_TYPE_OPTIONS);
    const opts = customerTypeOptionsFor('Builder');
    expect(opts.map((o) => o.value)).toEqual([...CUSTOMER_TYPES, 'Builder']);
  });
});
