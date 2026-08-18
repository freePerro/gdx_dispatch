// The customer-facing label for a captured door photo — the door size in
// Doug's format (16' × 7'). Best-effort: the spec shape belongs to the plugin
// (ADR-013 — line_metadata is deliberately generic), so probe the common
// width/height keys, then fall back to the first WxH in the description.
//
// Every path is fenced to a BELIEVABLE garage door — 4–24' wide, 6–24' tall,
// in whole feet — because a wrong size on a customer's photo is worse than no
// label. Pixel dimensions in metadata (800×600), lumber in a description
// ("2x4s"), and a 7'6" special all return blank for the office to hand-label
// in the attachment panel; the label is a prefill, never an authority.
export function doorSizeLabel(draft) {
  const meta = draft?.line_metadata || {};
  const spec = meta.spec || meta.door_spec || meta.door || meta;
  const toFeet = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return null;
    // Door dimensions in feet are single/low-double digits; anything larger
    // is inches (the labor matrix stores inches in *_ft columns — same trap).
    const ft = n > 30 ? n / 12 : n;
    const whole = Math.round(ft);
    // Not whole feet (7'6" → 7.5) → not confident enough to auto-label.
    return Math.abs(ft - whole) <= 0.05 ? whole : null;
  };
  const isDoorSize = (w, h) => w != null && h != null && w >= 4 && w <= 24 && h >= 6 && h <= 24;
  const w = toFeet(spec.width_ft ?? spec.width_in ?? spec.width);
  const h = toFeet(spec.height_ft ?? spec.height_in ?? spec.height);
  if (isDoorSize(w, h)) return `${w}' × ${h}'`;
  // Digit fences so "16x750" or a model number never matches.
  const m = /(?:^|[^\d])(\d{1,2})\s*[xX×]\s*(\d{1,2})(?!\d)/.exec(draft?.description || "");
  if (m && isDoorSize(Number(m[1]), Number(m[2]))) return `${m[1]}' × ${m[2]}'`;
  return "";
}
