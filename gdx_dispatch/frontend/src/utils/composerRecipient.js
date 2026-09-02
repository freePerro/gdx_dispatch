/**
 * Who a composer send actually goes to — one decision, one place.
 *
 * The bug this exists to prevent: `previewComposer` sent `to_email` and
 * `sendComposer` did not. The operator typed an address, watched the preview
 * render to it, pressed Send, and got "customer_has_no_email" back — the
 * server never saw the address. Two hand-built payload literals three lines
 * apart, and only one of them carried the field.
 *
 * So the payload is BUILT here, not assembled at each call site. Deleting
 * `to_email` from a send now means deleting it from the preview too, and the
 * unit tests fail either way. (EstimateView is 3,900 lines and needs the
 * pricing engine, catalogs and a route to mount, so a mounted request-body
 * assertion is not available — this is how the value gets asserted instead.)
 */

// Mirrors core/validation._EMAIL_RE closely enough to catch typos before a
// round trip. The server's `invalid_override` remains the authority.
const EMAIL_RE = /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/;

export function isValidRecipientEmail(addr) {
  const a = String(addr || "").trim();
  return EMAIL_RE.test(a) && !a.includes("..");
}

/**
 * The address a send would use, or "" when the saved-contact picker is driving.
 * Two ways to reach it: the account has no stored recipients at all (the
 * original free-text case), or the operator explicitly chose to override.
 */
export function typedAddress(composer) {
  const c = composer || {};
  const overriding = c.overrideMode || !(c.recipients || []).length;
  return overriding ? String(c.to || "").trim() : "";
}

/**
 * The exact body POSTed to /send and /email-preview. `extra` carries the
 * per-surface fields (estimates attach documents; invoices do not).
 */
export function composerSendPayload(composer, extra = {}) {
  const c = composer || {};
  return {
    body_text: c.body_text,
    subject: c.subject,
    contact_id: c.contact_id || null,
    to_email: typedAddress(c) || null,
    ...extra,
  };
}
