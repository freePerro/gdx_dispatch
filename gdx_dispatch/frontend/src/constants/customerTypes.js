// Canonical customer-type vocabulary for every form that edits the field.
// GDXA-413: the CustomerDetail edit dialog carried its own two-item list
// (Residential, Commercial) beside CustomerFormDialog's six, so a Contractor
// or Wholesale customer opened to a blank Select there and lost its type the
// moment the field was touched. Both forms import THIS list now.
//
// The API stores customer_type as free text (routers/customers.py), so this is
// a UI vocabulary, not a server enum: a value outside it is shown verbatim.
export const CUSTOMER_TYPES = [
  'Residential',
  'Commercial',
  'Retail',
  'Contractor',
  'Wholesale',
  'Property Manager',
];

export const CUSTOMER_TYPE_OPTIONS = CUSTOMER_TYPES.map((t) => ({ label: t, value: t }));

// Map a stored spelling ("contractor", "property_manager") to its canonical
// label. Unknown values come back verbatim, never masked as Residential —
// that hid 322 of 326 GDX customer rows from view 2026-04-29.
export function normalizeCustomerType(type, fallback = 'Residential') {
  const text = (type || '').toString().trim().toLowerCase().replace(/_/g, ' ');
  const known = CUSTOMER_TYPES.find((t) => t.toLowerCase() === text);
  if (known) return known;
  return type ? String(type) : fallback;
}

// Options for a Select editing `current`: the canonical list, plus `current`
// itself when it is a value outside the list, so the field never renders
// blank and a save without touching it keeps what the record held.
export function customerTypeOptionsFor(current) {
  if (!current || CUSTOMER_TYPES.includes(current)) return CUSTOMER_TYPE_OPTIONS;
  return [...CUSTOMER_TYPE_OPTIONS, { label: String(current), value: current }];
}
