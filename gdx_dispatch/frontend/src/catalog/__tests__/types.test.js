import { describe, expect, it } from 'vitest';
import {
  PRODUCT_CLASSES,
  PRODUCT_CLASS_OPTIONS,
  emptyItemForClass,
  getProductClass,
  readField,
  writeField,
} from '../types.js';

describe('catalog/types registry', () => {
  it('exposes parts and door classes at minimum', () => {
    expect(PRODUCT_CLASSES.parts).toBeDefined();
    expect(PRODUCT_CLASSES.door).toBeDefined();
  });

  it('door class includes install attributes in form fields', () => {
    const fields = PRODUCT_CLASSES.door.formFields.map((f) => f.name);
    expect(fields).toContain('spec.width');
    expect(fields).toContain('spec.height');
    expect(fields).toContain('spec.r_value');
    expect(fields).toContain('spec.panel_style');
    expect(fields).toContain('spec.color');
  });

  it('parts class has no spec fields (back-compat)', () => {
    const fields = PRODUCT_CLASSES.parts.formFields.map((f) => f.name);
    expect(fields.every((n) => !n.startsWith('spec.'))).toBe(true);
  });

  it('PRODUCT_CLASS_OPTIONS is shaped for PrimeVue Select', () => {
    expect(PRODUCT_CLASS_OPTIONS.length).toBeGreaterThanOrEqual(2);
    PRODUCT_CLASS_OPTIONS.forEach((opt) => {
      expect(opt).toHaveProperty('label');
      expect(opt).toHaveProperty('value');
    });
  });

  it('emptyItemForClass(door) seeds a spec object', () => {
    const item = emptyItemForClass('door');
    expect(item.spec).toBeDefined();
    expect(typeof item.spec).toBe('object');
  });

  it('emptyItemForClass(parts) does not include spec', () => {
    const item = emptyItemForClass('parts');
    expect(item.spec).toBeUndefined();
  });

  it('getProductClass falls back to parts for unknown keys', () => {
    expect(getProductClass('nonsense').key).toBe('parts');
  });

  it('readField/writeField traverse nested spec paths', () => {
    const obj = { name: 'x', spec: { width: 96 } };
    expect(readField(obj, 'name')).toBe('x');
    expect(readField(obj, 'spec.width')).toBe(96);

    writeField(obj, 'spec.color', 'white');
    expect(obj.spec.color).toBe('white');

    writeField(obj, 'spec.nested.deep', 1);
    expect(obj.spec.nested.deep).toBe(1);
  });
});
