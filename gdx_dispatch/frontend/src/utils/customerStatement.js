/**
 * Customer statement helpers.
 *
 * Presets resolve on the server so the preview, the PDF and the email all
 * cover the same days on the shop's calendar. The only date math here turns a
 * picked calendar day into YYYY-MM-DD without going through UTC —
 * toISOString() moves a US evening onto the next day and a local midnight east
 * of UTC onto the previous one.
 */

export const DEFAULT_PRESET = 'last_90';

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

export function toIsoDay(value) {
  if (typeof value === 'string') return ISO_DAY.test(value) ? value : '';
  if (!(value instanceof Date) || Number.isNaN(value.getTime())) return '';
  const y = value.getFullYear();
  const m = String(value.getMonth() + 1).padStart(2, '0');
  const d = String(value.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** The query string (and send body) for a preset or a custom range. */
export function statementParams(preset, customStart, customEnd) {
  if (preset === 'custom') {
    const start = toIsoDay(customStart);
    const end = toIsoDay(customEnd);
    if (!start || !end) return null;
    return { start, end };
  }
  return { preset: preset || DEFAULT_PRESET };
}

export function statementQuery(params) {
  return new URLSearchParams(params).toString();
}

/**
 * The recipient part of a send. The prefilled address is the server's own
 * choice (a picked or primary contact, else the account email), so sending it
 * back would turn it into a typed override — losing the contact's name in the
 * greeting and the contact on the email log. Only an edited address is sent;
 * the invoice composer learned the same lesson (utils/composerRecipient.js).
 */
export function recipientPayload(typed, defaultEmail) {
  const t = String(typed || '').trim();
  if (!t || t.toLowerCase() === String(defaultEmail || '').trim().toLowerCase()) return {};
  return { to_email: t };
}

const SKIP_MESSAGES = {
  exception: 'Something went wrong producing or sending the statement. It was not sent; try again or download the PDF.',
  customer_has_no_email: 'This customer has no email address. Type one above, or download the PDF.',
  invalid_recipient_email: "That email address doesn't look right.",
  duplicate_send_suppressed: 'A statement was just sent to this customer. Wait a moment before sending again.',
  no_email_provider_connected:
    'No email account is connected for you. Connect Outlook under Settings, or download the PDF and send it yourself.',
  outlook_not_connected:
    'No email account is connected for you. Connect Outlook under Settings, or download the PDF and send it yourself.',
  outlook_reconnect_required: 'Your Outlook connection needs to be renewed under Settings.',
  send_failed: 'The email provider refused the message. Try again, or download the PDF.',
};

export function skipReasonMessage(reason) {
  if (!reason) return 'The statement was not sent.';
  return SKIP_MESSAGES[reason] || `The statement was not sent (${reason}).`;
}
