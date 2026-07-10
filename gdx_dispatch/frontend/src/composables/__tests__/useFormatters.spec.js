import { describe, it, expect } from 'vitest';
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatPercent,
  formatNumber,
  formatPhone,
} from '../useFormatters';

const LOCALE = 'en-US';

describe('formatPhone', () => {
  it('formats a clean 10-digit number as (111)222-3333', () => {
    expect(formatPhone('1112223333')).toBe('(111)222-3333');
  });

  it('normalizes numbers that already have separators or a country code', () => {
    expect(formatPhone('111-222-3333')).toBe('(111)222-3333');
    expect(formatPhone('(111) 222-3333')).toBe('(111)222-3333');
    expect(formatPhone('+1 111 222 3333')).toBe('(111)222-3333');
    expect(formatPhone('11112223333')).toBe('(111)222-3333');
  });

  it('returns empty string for null/undefined/empty (no placeholder)', () => {
    expect(formatPhone(null)).toBe('');
    expect(formatPhone(undefined)).toBe('');
    expect(formatPhone('')).toBe('');
  });

  it('passes through non-10-digit values unchanged (never mangles)', () => {
    expect(formatPhone('555-1234')).toBe('555-1234'); // 7 digits
    expect(formatPhone('1112223333x99')).toBe('1112223333x99'); // extension
    expect(formatPhone('+44 20 7946 0958')).toBe('+44 20 7946 0958'); // intl
  });
});

describe('useFormatters', () => {
  it('formatDate handles null/undefined/empty', () => {
    expect(formatDate(null)).toBe('—');
    expect(formatDate(undefined)).toBe('—');
    expect(formatDate('')).toBe('—');
    expect(formatDate('not-a-date')).toBe('—');
  });

  it('formatDate renders ISO string', () => {
    expect(formatDate('2026-05-09T12:00:00Z', { locale: LOCALE })).toMatch(/May 9, 2026/);
  });

  it('formatDate accepts Date and epoch', () => {
    const d = new Date('2026-01-15T00:00:00Z');
    expect(formatDate(d, { locale: LOCALE })).toMatch(/Jan 1[45], 2026/);
    expect(formatDate(d.getTime(), { locale: LOCALE })).toMatch(/Jan 1[45], 2026/);
  });

  it('formatDateTime includes hour:minute', () => {
    const out = formatDateTime('2026-05-09T12:30:00Z', { locale: LOCALE });
    expect(out).toMatch(/2026/);
    expect(out).toMatch(/[0-9]{1,2}:[0-9]{2}/);
  });

  it('formatMoney USD default', () => {
    expect(formatMoney(1234.5, { locale: LOCALE })).toBe('$1,234.50');
    expect(formatMoney('99', { locale: LOCALE })).toBe('$99.00');
    expect(formatMoney(0, { locale: LOCALE })).toBe('$0.00');
  });

  it('formatMoney handles null + non-numeric', () => {
    expect(formatMoney(null)).toBe('—');
    expect(formatMoney(undefined)).toBe('—');
    expect(formatMoney('abc')).toBe('—');
    expect(formatMoney(NaN)).toBe('—');
  });

  it('formatMoney respects digits option', () => {
    expect(formatMoney(1234, { locale: LOCALE, digits: 0 })).toBe('$1,234');
  });

  it('formatPercent fraction default', () => {
    expect(formatPercent(0.255, { locale: LOCALE, digits: 1 })).toBe('25.5%');
    expect(formatPercent(0, { locale: LOCALE })).toBe('0.0%');
  });

  it('formatPercent whole option', () => {
    expect(formatPercent(25, { locale: LOCALE, whole: true, digits: 0 })).toBe('25%');
  });

  it('formatPercent handles null', () => {
    expect(formatPercent(null)).toBe('—');
  });

  it('formatNumber thousands separator', () => {
    expect(formatNumber(1234567, { locale: LOCALE })).toBe('1,234,567');
  });
});
