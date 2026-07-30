// Plan §9/§8: the JS lane resolver must fold legacy spellings exactly like
// core/job_taxonomy.pricing_lane — a hardcoded `=== 'Installation'` (the bug
// this replaced) missed the quote flow's lowercase 'installation'.
import { describe, expect, it } from 'vitest';
import { canonicalJobType, isInstallLane, isServiceLane, pricingLane } from '../jobTypes';

describe('job-type lane resolver', () => {
  it('folds every install spelling to the install lane', () => {
    for (const raw of ['Installation', 'installation', 'Install', 'INSTALL']) {
      expect(pricingLane(raw)).toBe('install');
      expect(isInstallLane(raw)).toBe(true);
    }
  });
  it('folds service spellings to the service lane', () => {
    for (const raw of ['Service Call', 'Service', 'service', 'Repair', 'Maintenance']) {
      expect(pricingLane(raw)).toBe('service');
      expect(isServiceLane(raw)).toBe(true);
    }
  });
  it('routes unknown / QB Import / null to office', () => {
    for (const raw of ['QB Import', 'New Construction', 'Inspection', 'mystery', null, '']) {
      expect(pricingLane(raw)).toBe('office');
      expect(isInstallLane(raw)).toBe(false);
    }
  });
  it('canonicalizes but never guesses unknowns', () => {
    expect(canonicalJobType('installation')).toBe('Installation');
    expect(canonicalJobType('Garage Cleanout')).toBe('Garage Cleanout');
    expect(canonicalJobType(null)).toBe(null);
  });
});
