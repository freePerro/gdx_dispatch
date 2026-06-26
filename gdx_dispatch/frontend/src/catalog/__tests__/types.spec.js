/**
 * ADR-015 — custom catalog registry logic. These pure functions turn an
 * API-provided field_schema into the form/table definitions the view renders,
 * so a custom catalog needs no static registry entry.
 */
import { describe, expect, it } from 'vitest';
import {
  PRODUCT_CLASS_OPTIONS,
  getCatalogClass,
  getProductClass,
  buildCustomClass,
  emptyItemForCatalog,
  createPayloadFromPackType,
} from '../types.js';

const HVAC = {
  product_class: 'custom',
  name: 'HVAC Units',
  field_schema: [
    { name: 'tonnage', label: 'Tonnage', type: 'number' },
    { name: 'refrigerant', label: 'Refrigerant', type: 'select', options: ['R-410A', 'R-32'] },
    { name: 'energy_star', label: 'Energy Star', type: 'checkbox' },
  ],
};

describe('PRODUCT_CLASS_OPTIONS', () => {
  it('includes the Custom… choice', () => {
    const custom = PRODUCT_CLASS_OPTIONS.find((o) => o.value === 'custom');
    expect(custom).toBeTruthy();
    expect(custom.label).toMatch(/custom/i);
  });
});

describe('getCatalogClass', () => {
  it('builds a data-driven class for custom catalogs', () => {
    const klass = getCatalogClass(HVAC);
    expect(klass.isCustom).toBe(true);
    expect(klass.label).toBe('HVAC Units'); // catalog name, not a static label
  });

  it('falls back to the static registry for built-in classes', () => {
    expect(getCatalogClass({ product_class: 'door' }).key).toBe('door');
    expect(getCatalogClass({ product_class: 'parts' }).key).toBe('parts');
    // unknown class → parts fallback (matches getProductClass)
    expect(getCatalogClass({ product_class: 'nope' }).key).toBe(getProductClass('nope').key);
  });
});

describe('buildCustomClass', () => {
  const klass = buildCustomClass(HVAC);

  it('prefixes custom field paths with attributes.', () => {
    const names = klass.formFields.map((f) => f.name);
    // spine fields first, then attributes.*
    expect(names).toContain('name');
    expect(names).toContain('attributes.tonnage');
    expect(names).toContain('attributes.refrigerant');
  });

  it('maps field types to inputs and carries select options', () => {
    const refrigerant = klass.formFields.find((f) => f.name === 'attributes.refrigerant');
    expect(refrigerant.type).toBe('select');
    expect(refrigerant.options).toEqual(['R-410A', 'R-32']);
    const star = klass.formFields.find((f) => f.name === 'attributes.energy_star');
    expect(star.type).toBe('checkbox');
  });

  it('adds custom columns to the table between spine and pricing', () => {
    const fields = klass.tableColumns.map((c) => c.field);
    expect(fields).toContain('sku');
    expect(fields).toContain('attributes.tonnage');
    expect(fields).toContain('price');
  });
});

describe('createPayloadFromPackType (Slice 3)', () => {
  const packType = {
    key: 'hvac_unit',
    label: 'HVAC Units',
    field_schema: [{ name: 'tonnage', label: 'Tonnage', type: 'number' }],
    pricing_strategy: 'hvac_markup_40',
    pricing_config: { kind: 'markup', params: { pct: 0.4 } },
  };

  it('expands a pack type into a self-contained custom-catalog payload', () => {
    const payload = createPayloadFromPackType('My HVAC', 'manual', packType);
    expect(payload.product_class).toBe('custom');
    expect(payload.field_schema).toEqual(packType.field_schema);
    expect(payload.pricing_strategy).toBe('hvac_markup_40');
    expect(payload.pricing_config).toEqual({ kind: 'markup', params: { pct: 0.4 } });
    expect(payload.name).toBe('My HVAC');
  });
});

describe('emptyItemForCatalog', () => {
  it('gives custom catalogs an attributes bag', () => {
    const item = emptyItemForCatalog(HVAC);
    expect(item.attributes).toEqual({});
  });

  it('does not add attributes for parts catalogs', () => {
    const item = emptyItemForCatalog({ product_class: 'parts' });
    expect(item.attributes).toBeUndefined();
  });
});
