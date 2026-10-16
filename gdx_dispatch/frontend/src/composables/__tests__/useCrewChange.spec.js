import { describe, expect, it } from 'vitest';
import { crewChangeOptions, crewOf, crewPatch, useCrewChangePrompt } from '../useCrewChange';

const NAMES = { t1: 'Mike', t2: 'Sam', t3: 'Pat' };
const nameOf = (id) => NAMES[id] || id;

describe('crewOf', () => {
  it('puts the lead first', () => {
    expect(crewOf({ assigned_tech_ids: ['t1', 't2'], lead_tech_id: 't2' })).toEqual(['t2', 't1']);
  });
  it('folds in a legacy primary tech missing from the list', () => {
    expect(crewOf({ assigned_tech_ids: ['t2'], assigned_to: 't1' })).toEqual(['t1', 't2']);
  });
  it('is empty for an unassigned job', () => {
    expect(crewOf({})).toEqual([]);
  });
});

describe('crewChangeOptions', () => {
  it('offers a swap per member and an add; swapping the lead makes the new tech lead', () => {
    const opts = crewChangeOptions(['t1', 't2'], 't3', nameOf);
    expect(opts.map((o) => [o.key, o.tech_ids, o.lead])).toEqual([
      ['swap-t1', ['t3', 't2'], 't3'],
      ['swap-t2', ['t1', 't3'], 't1'],
      ['add', ['t1', 't2', 't3'], 't1'],
    ]);
    expect(crewPatch(opts[2])).toEqual({ assigned_tech_ids: ['t1', 't2', 't3'], lead_tech_id: 't1' });
  });
  it('offers the dragged-from member first', () => {
    expect(crewChangeOptions(['t1', 't2'], 't3', nameOf, 't2')[0].key).toBe('swap-t2');
  });
});

describe('useCrewChangePrompt', () => {
  it('a second question cancels the first, so nothing waits forever', async () => {
    const { ask, answer, prompt } = useCrewChangePrompt(nameOf);
    const first = ask({ crew: ['t1', 't2'], techId: 't3' });
    const second = ask({ crew: ['t1', 't2'], techId: 't3' });
    expect(await first).toBeNull();
    answer(prompt.value.options[0]);
    expect((await second).key).toBe('swap-t1');
    expect(prompt.value.visible).toBe(false);
  });
});
