// Pure helpers for rendering Phone.com call rows. Extracted from
// PhoneComCallsView so the list view, the detail modal, and unit tests all
// see the same logic.

const US_STATE_POSTAL = new Set([
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
  'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
  'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
  'VA','WA','WV','WI','WY','DC','PR',
]);

const CNAM_NOISE_EXACT = new Set([
  'WIRELESS CALLER',
  'UNKNOWN CALLER',
  'UNKNOWN',
  'UNAVAILABLE',
  'NAME UNAVAILABLE',
  'OUT OF AREA',
  'RESTRICTED',
  'PRIVATE CALLER',
  'PRIVATE NAME',
  'NO CALLER ID',
  'ANONYMOUS',
  'BLOCKED',
  'TOLL FREE',
  'WIRELESS CL',
]);

// CNAM is "junk" if it's geographic (CITY ST), one of the known noise strings,
// or matches the bare phone number itself.
export function isCnamJunk(cnam, fromNumber) {
  if (!cnam) return true;
  const trimmed = cnam.trim();
  if (!trimmed) return true;
  if (fromNumber && trimmed === fromNumber) return true;
  if (fromNumber && trimmed.replace(/\D/g, '') === fromNumber.replace(/\D/g, '')) return true;
  const upper = trimmed.toUpperCase();
  if (CNAM_NOISE_EXACT.has(upper)) return true;
  // "TROY NY", "EMHOUSE TX", "SEBEKA MN" — all-caps WORDS followed by a
  // 2-letter US state code at the end. Phone.com falls back to this when no
  // real CNAM is available.
  const stateSuffix = /^[A-Z][A-Z .'-]*\s([A-Z]{2})$/;
  const m = upper.match(stateSuffix);
  if (m && US_STATE_POSTAL.has(m[1])) return true;
  return false;
}

export function callerDisplay(call) {
  const cnam = call?.caller_cnam;
  const num = call?.from_number || '';
  if (cnam && !isCnamJunk(cnam, num)) {
    return `${cnam}${num ? ` · ${num}` : ''}`.trim();
  }
  return num || '—';
}

// Wave F / S11 — backend now normalizes status into a clean enum
// (voicemail|forwarded|answered|missed|canceled) before storage. This
// mapping just title-cases. Pre-S11 raw values are still tolerated until
// the backfill UPDATE lands.
const _STATUS_LABELS = {
  voicemail: 'Voicemail',
  forwarded: 'Forwarded',
  answered: 'Completed',
  missed: 'Missed',
  canceled: 'Canceled',
};

export function friendlyStatus(call) {
  const raw = (call?.status || '').toLowerCase();
  if (!raw) return '—';
  if (_STATUS_LABELS[raw]) return _STATUS_LABELS[raw];
  // Pre-S11 backfill: tolerate the old raw shapes.
  if (raw.includes('voicemail_received') || raw.includes('voicemail received')) return 'Voicemail';
  if (raw.includes('voicemail')) return 'Voicemail';
  if (raw.startsWith('dial_out') || raw.includes('forwarded')) return 'Forwarded';
  if (raw.includes('answered') || raw.includes('completed')) return 'Completed';
  if (raw.includes('missed') || raw.includes('busy') || raw.includes('no_answer')) return 'Missed';
  if (raw.includes('canceled') || raw.includes('hung')) return 'Canceled';
  return (call.status || '').replace(/^type\s+/, '').replace(/_/g, ' ');
}

export function prettyDirection(direction) {
  if (direction === 'in') return 'Inbound';
  if (direction === 'out') return 'Outbound';
  return direction || '—';
}

// Wave C / S12 — render an own-number as its label (or "Main") instead of
// the bare DID. `ownNumbers` is the list of {phone_com_number, label} loaded
// once on view mount; called per row in the list/detail.
export function renderOwnNumber(num, ownNumbers) {
  if (!num) return '';
  const match = (ownNumbers || []).find(n => n.phone_com_number === num);
  if (!match) return num;
  return match.label ? `${match.label} (${num})` : `Main (${num})`;
}
