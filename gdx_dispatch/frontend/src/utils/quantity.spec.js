import { describe, expect, it } from 'vitest';
import { recordedQuantity } from './quantity';

describe('recordedQuantity (#560)', () => {
  it('reads a blank quantity as 1 and a recorded 0 as 0', () => {
    expect(recordedQuantity(undefined)).toBe(1);
    expect(recordedQuantity(null)).toBe(1);
    expect(recordedQuantity('')).toBe(1);
    expect(recordedQuantity(0)).toBe(0);
    expect(recordedQuantity('0')).toBe(0);
    expect(recordedQuantity('2.5')).toBe(2.5);
    expect(recordedQuantity(3)).toBe(3);
  });

  it('does not turn a value that is not a number into 1', () => {
    expect(recordedQuantity('abc')).toBeNaN();
  });
});
