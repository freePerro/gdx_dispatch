import { describe, it, expect } from 'vitest';
import {
  isCnamJunk,
  callerDisplay,
  friendlyStatus,
  prettyDirection,
  renderOwnNumber,
} from '../src/utils/phoneComLabels.js';

describe('isCnamJunk', () => {
  it('flags geographic CNAM (CITY ST)', () => {
    expect(isCnamJunk('TROY NY', '+15188804348')).toBe(true);
    expect(isCnamJunk('EMHOUSE TX', '+19033544502')).toBe(true);
    expect(isCnamJunk('SEBEKA MN', '+12185394010')).toBe(true);
    expect(isCnamJunk('HUDSON NY', '+15188223469')).toBe(true);
  });

  it('flags wireless / unknown / restricted patterns', () => {
    expect(isCnamJunk('WIRELESS CALLER', '+17632136017')).toBe(true);
    expect(isCnamJunk('UNKNOWN CALLER', '+10000000000')).toBe(true);
    expect(isCnamJunk('RESTRICTED', '+10000000000')).toBe(true);
    expect(isCnamJunk('OUT OF AREA', '+10000000000')).toBe(true);
    expect(isCnamJunk('ANONYMOUS', '+10000000000')).toBe(true);
  });

  it('flags CNAM equal to the phone number', () => {
    expect(isCnamJunk('+15188804348', '+15188804348')).toBe(true);
    expect(isCnamJunk('15188804348', '+15188804348')).toBe(true);
  });

  it('keeps real names', () => {
    expect(isCnamJunk('MEINECKE R.', '+13202959628')).toBe(false);
    expect(isCnamJunk('SAWYER LEON', '+13204241164')).toBe(false);
    expect(isCnamJunk('Becky Mienke', '+13202959628')).toBe(false);
  });

  it('handles empty / null', () => {
    expect(isCnamJunk('', '+1')).toBe(true);
    expect(isCnamJunk(null, '+1')).toBe(true);
    expect(isCnamJunk('   ', '+1')).toBe(true);
  });

  it('does NOT flag two-letter words that are not US state codes', () => {
    expect(isCnamJunk('SMITH JR', '+10000000000')).toBe(false);
  });
});

describe('callerDisplay', () => {
  it('renders CNAM · number for real CNAM', () => {
    expect(
      callerDisplay({ caller_cnam: 'MEINECKE R.', from_number: '+13202959628' }),
    ).toBe('MEINECKE R. · +13202959628');
  });

  it('falls back to bare number for junk CNAM', () => {
    expect(
      callerDisplay({ caller_cnam: 'TROY NY', from_number: '+15188804348' }),
    ).toBe('+15188804348');
    expect(
      callerDisplay({ caller_cnam: 'WIRELESS CALLER', from_number: '+17632136017' }),
    ).toBe('+17632136017');
  });

  it('shows em-dash when no number and junk CNAM', () => {
    expect(callerDisplay({ caller_cnam: 'RESTRICTED', from_number: '' })).toBe('—');
  });

  it('shows bare number when CNAM missing', () => {
    expect(callerDisplay({ caller_cnam: null, from_number: '+12345' })).toBe('+12345');
  });
});

describe('friendlyStatus', () => {
  it('maps voicemail final_action variants', () => {
    expect(friendlyStatus({ status: 'type voicemail_received' })).toBe('Voicemail');
    expect(friendlyStatus({ status: 'voicemail received' })).toBe('Voicemail');
  });

  it('maps dial_out and forwarded to Forwarded', () => {
    expect(friendlyStatus({ status: 'dial_out +13202325143' })).toBe('Forwarded');
    expect(friendlyStatus({ status: 'forwarded to extension 100' })).toBe('Forwarded');
  });

  it('maps answered/completed to Completed', () => {
    expect(friendlyStatus({ status: 'answered' })).toBe('Completed');
    expect(friendlyStatus({ status: 'completed' })).toBe('Completed');
  });

  it('maps missed/busy/no_answer to Missed', () => {
    expect(friendlyStatus({ status: 'missed' })).toBe('Missed');
    expect(friendlyStatus({ status: 'busy' })).toBe('Missed');
    expect(friendlyStatus({ status: 'no_answer' })).toBe('Missed');
  });

  it('returns em-dash for null/empty', () => {
    expect(friendlyStatus({ status: null })).toBe('—');
    expect(friendlyStatus({ status: '' })).toBe('—');
    expect(friendlyStatus({})).toBe('—');
  });
});

describe('renderOwnNumber', () => {
  const own = [
    { phone_com_number: '+18005550199', label: 'Main' },
    { phone_com_number: '+13201234567', label: null },
  ];

  it('renders label + DID when own with label', () => {
    expect(renderOwnNumber('+18005550199', own)).toBe('Main (+18005550199)');
  });

  it('renders Main fallback when own without label', () => {
    expect(renderOwnNumber('+13201234567', own)).toBe('Main (+13201234567)');
  });

  it('passes through DID when not own', () => {
    expect(renderOwnNumber('+19998887777', own)).toBe('+19998887777');
  });

  it('handles empty / null inputs', () => {
    expect(renderOwnNumber('', own)).toBe('');
    expect(renderOwnNumber(null, own)).toBe('');
    expect(renderOwnNumber('+18005550199', null)).toBe('+18005550199');
    expect(renderOwnNumber('+18005550199', [])).toBe('+18005550199');
  });
});

describe('prettyDirection', () => {
  it('expands in/out', () => {
    expect(prettyDirection('in')).toBe('Inbound');
    expect(prettyDirection('out')).toBe('Outbound');
  });

  it('passes through unknown / null', () => {
    expect(prettyDirection('inbound')).toBe('inbound');
    expect(prettyDirection(null)).toBe('—');
    expect(prettyDirection('')).toBe('—');
  });
});
