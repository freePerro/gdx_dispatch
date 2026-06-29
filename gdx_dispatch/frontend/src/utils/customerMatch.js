// Pure, high-precision duplicate-customer matching for the at-entry "did you
// mean?" warning in CustomerFormDialog. Kept free of Vue/HTTP so the matching
// rules are unit-testable in isolation.
//
// Design bias: PRECISION over recall. A false-positive warning trains users to
// ignore it, so we only flag STRONG matches — same phone, same email, or the
// exact same normalized name — never fuzzy/near matches. The warning is
// non-blocking; the user can still save (it may genuinely be a different
// person who shares a name).

export function normalizePhone(value) {
  const digits = (value || "").toString().replace(/\D/g, "");
  // Drop a leading US country code so "+1 (555) 123-4567" and "(555) 123-4567"
  // compare equal, and a digits query matches a stored number regardless of
  // which form was saved (the backend's substring LIKE matches a 10-digit
  // needle inside an 11-digit "1…" stored value, but not vice-versa).
  return digits.length === 11 && digits.startsWith("1") ? digits.slice(1) : digits;
}

export function normalizeName(value) {
  return (value || "").toString().trim().toLowerCase().replace(/\s+/g, " ");
}

export function normalizeEmail(value) {
  return (value || "").toString().trim().toLowerCase();
}

// Find the first existing customer that strongly matches the candidate.
// Returns { customer, on } where `on` is 'phone' | 'email' | 'name', or null.
// Checks are ordered by confidence (phone > email > name) and applied across
// the whole list per criterion, so a phone match always wins over a coincidental
// name collision later in the list.
//   options.excludeId  skip this id (edit mode: don't match the record itself)
export function findDuplicateMatch(candidate, list, options = {}) {
  const excludeId = options.excludeId ?? null;
  const phone = normalizePhone(candidate?.phone);
  const email = normalizeEmail(candidate?.email);
  const name = normalizeName(candidate?.name);

  const pool = (Array.isArray(list) ? list : []).filter(
    (c) => excludeId == null || String(c?.id) !== String(excludeId),
  );

  // Phone: require >=7 digits so a partial area code / typo doesn't match.
  if (phone.length >= 7) {
    const m = pool.find((c) => normalizePhone(c?.phone) === phone);
    if (m) return { customer: m, on: "phone" };
  }
  if (email) {
    const m = pool.find((c) => normalizeEmail(c?.email) === email);
    if (m) return { customer: m, on: "email" };
  }
  // Name: require >=3 chars so single-initial noise doesn't match.
  if (name.length >= 3) {
    const m = pool.find((c) => normalizeName(c?.name) === name);
    if (m) return { customer: m, on: "name" };
  }
  return null;
}

// The strongest identifier to send as the /api/customers?q= lookup term:
// phone (digits) if usably long, else email, else name. Returns "" when there
// is nothing worth querying yet (so the dialog can skip the call entirely).
export function bestLookupTerm(candidate) {
  const phone = normalizePhone(candidate?.phone);
  if (phone.length >= 7) return phone;
  const email = normalizeEmail(candidate?.email);
  if (email) return email;
  const name = normalizeName(candidate?.name);
  if (name.length >= 3) return name;
  return "";
}

// All identifiers worth querying, strongest first. The dialog queries each and
// merges the candidate pools, so a duplicate keyed on phone is still found even
// when the typed name differs (and vice-versa) — a single "best" term would
// miss cross-field dupes. Empty array = nothing worth querying yet.
export function lookupTerms(candidate) {
  const terms = [];
  const phone = normalizePhone(candidate?.phone);
  if (phone.length >= 7) terms.push(phone);
  const email = normalizeEmail(candidate?.email);
  if (email) terms.push(email);
  const name = normalizeName(candidate?.name);
  if (name.length >= 3) terms.push(name);
  return terms;
}
