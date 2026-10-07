import { describe, expect, it } from 'vitest';
import { lineAmount, recordedQuantity } from './quantity';

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

describe('lineAmount (migration 108)', () => {
  it('rounds a half cent up exactly as the server does, not as the float lands', () => {
    // 2.5 × 33.33 is 83.32499999999999 as a float; the server bills 83.33.
    expect(2.5 * 33.33).toBeLessThan(83.325);
    expect(lineAmount(2.5, 33.33)).toBe(83.33);
    expect(lineAmount(0.25, 0.1)).toBe(0.03); // 0.025 → 0.03
    expect(lineAmount(1.5, 1.01)).toBe(1.52); // 1.515 → 1.52
  });

  it('matches plain multiplication for whole quantities', () => {
    expect(lineAmount(3, 149)).toBe(447);
    expect(lineAmount(2, 75.5)).toBe(151);
    expect(lineAmount('4', '12.25')).toBe(49);
  });

  it('rounds a negative half away from zero (ROUND_HALF_UP)', () => {
    expect(lineAmount(2.5, -33.33)).toBe(-83.33);
  });

  it('reads anything that is not a number as $0', () => {
    expect(lineAmount(null, 10)).toBe(0);
    expect(lineAmount('abc', 10)).toBe(0);
    expect(lineAmount(2, undefined)).toBe(0);
  });
});
