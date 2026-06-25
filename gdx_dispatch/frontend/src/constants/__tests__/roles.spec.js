/**
 * Canonical role normalization — constants/roles.js (mirror of core/roles.py).
 */
import { describe, it, expect } from 'vitest';
import {
  normalizeRole, isTechnician, isAdminTier, isOwner, humanizeRole,
  TECHNICIAN, DISPATCHER, SUPER_ADMIN, OWNER,
} from '../roles';

describe('normalizeRole', () => {
  it.each([
    ['tech', TECHNICIAN], ['technician', TECHNICIAN], ['TECH', TECHNICIAN],
    ['dispatch', DISPATCHER], ['dispatcher', DISPATCHER],
    ['superadmin', SUPER_ADMIN], ['super_admin', SUPER_ADMIN], ['super-admin', SUPER_ADMIN],
    ['  Owner ', OWNER], ['admin', 'admin'],
    [null, ''], [undefined, ''], ['', ''], ['weird', 'weird'],
  ])('normalizes %s → %s', (raw, expected) => {
    expect(normalizeRole(raw)).toBe(expected);
  });
});

describe('isTechnician', () => {
  it('accepts both legacy and canonical spelling', () => {
    expect(isTechnician('tech')).toBe(true);
    expect(isTechnician('technician')).toBe(true);
    expect(isTechnician('TECH')).toBe(true);
  });
  it('rejects non-tech roles', () => {
    for (const r of ['dispatcher', 'dispatch', 'admin', 'owner', 'sales', '', null]) {
      expect(isTechnician(r)).toBe(false);
    }
  });
});

describe('isAdminTier', () => {
  it('true for owner/admin/superadmin variants', () => {
    for (const r of ['owner', 'admin', 'super_admin', 'super-admin', 'superadmin']) {
      expect(isAdminTier(r)).toBe(true);
    }
  });
  it('false for office/field roles', () => {
    for (const r of ['dispatcher', 'dispatch', 'tech', 'technician', 'sales', 'viewer', '']) {
      expect(isAdminTier(r)).toBe(false);
    }
  });
});

describe('isOwner (owner tier — owner/superadmin, NOT admin)', () => {
  it('true for owner + all superadmin spellings', () => {
    for (const r of ['owner', 'super_admin', 'superadmin', 'super-admin']) {
      expect(isOwner(r)).toBe(true);
    }
  });
  it('false for admin and everyone else', () => {
    for (const r of ['admin', 'dispatcher', 'dispatch', 'tech', 'technician', 'sales', 'viewer', '', null]) {
      expect(isOwner(r)).toBe(false);
    }
  });
});

describe('humanizeRole', () => {
  it('maps canonical + legacy spellings to a friendly label', () => {
    expect(humanizeRole('tech')).toBe('Technician');
    expect(humanizeRole('dispatch')).toBe('Dispatcher');
    expect(humanizeRole('super_admin')).toBe('Super admin');
    expect(humanizeRole('owner')).toBe('Owner');
  });
  it('title-cases unknown roles and handles empty', () => {
    expect(humanizeRole('partner')).toBe('Partner');
    expect(humanizeRole('')).toBe('');
    expect(humanizeRole(null)).toBe('');
  });
});
