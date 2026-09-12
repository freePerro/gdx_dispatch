/**
 * #560 — a stored quantity of 0 is 0, not 1.
 *
 * `quantity || 1` turned a recorded zero into one: a zero-quantity line
 * showed "×1", totalled one unit, and was copied into invoices as one — the
 * invoice editor even rewrote a stored 0 as 1 on save. Stored quantities go
 * through `utils/quantity.recordedQuantity` (blank → 1, 0 → 0).
 *
 * Forms that normalise what someone TYPES for a new line are a different
 * decision (the owner ruled on reading stored values), so the handful that
 * clamp to at least 1 are listed below with why. Anything else fails here.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');
const QUANTITY_OR_ONE = /\b(quantity|qty)\)?\s*\|\|\s*1\b/;
// `v-if="line.quantity > 1"` hides a recorded 0, so it reads as one unit —
// the same re-rating by omission. Five renderers did this (#560 round 2).
const QUANTITY_GREATER_THAN_ONE = /\b(quantity|qty)\s*>\s*1\b/;
// `v-if="p.quantity"` is the same hiding by another spelling: 0 is falsy.
const VIF = /v-if="([^"]*)"/g;
function hidesAZeroQuantity(line) {
  for (const [, expr] of line.matchAll(VIF)) {
    if (!/\b(quantity|qty)\b/.test(expr)) continue;
    if (/[=<>!]|recordedQuantity|\?\?/.test(expr)) continue; // compared or resolved — fine
    return true;
  }
  return false;
}

// `${file}::${trimmed line}` -> why it is input normalisation, not a stored read.
const ALLOWED = {
  'views/MobileTodayView.vue::quantity: Number(partsForm.value.quantity) || 1,': 'parts form: what the tech types',
  'components/MobileJobCloseoutDialog.vue::qty: Number(p.qty) || 1,': 'closeout form: parts the tech types',
  'components/MobileJobCloseoutDialog.vue::qty: Math.min(99, Math.max(1, Math.trunc(Number(p.qty) || 1))),':
    'closeout form: clamps a typed quantity to 1..99',
  'components/MobileJobNewDialog.vue::quantity: Number(p.quantity) || 1,': 'new-job form: parts being typed',
  'views/JobsView.vue::quantity: Math.max(1, Number(p.quantity) || 1),': 'new-job form: clamps a typed quantity',
  'views/JobDetailView.vue::quantity: Math.max(1, Number(f.quantity) || 1),': 'order-a-part form: clamps a typed quantity',
  'views/EstimateView.vue::li.quantity = draft.quantity || 1;': 'a picker draft being inserted, not a stored line',
  'views/EstimateView.vue::quantity: line.quantity || 1,': 'AI-suggested lines, not stored ones',
  'views/EstimateView.vue::quantity: li.qty || 1,': 'AI instant-estimate output, not stored lines',
};

function isCommentLine(line) {
  const s = line.trim();
  return s.startsWith('//') || s.startsWith('*') || s.startsWith('/*') || s.startsWith('<!--');
}

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.') || entry === '__tests__') continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (full.endsWith('.vue') || (full.endsWith('.js') && !full.endsWith('.spec.js'))) out.push(full);
  }
  return out;
}

describe('no stored quantity is read with || 1 (#560)', () => {
  it('no stored quantity is re-rated by `|| 1` or hidden by `> 1`, and every ALLOWED entry still exists', () => {
    const found = new Set();
    const violations = [];
    for (const file of walk(SRC_DIR)) {
      const rel = relative(SRC_DIR, file);
      readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
        if (isCommentLine(line)) return;
        if (hidesAZeroQuantity(line)) {
          violations.push(`${rel}:${i + 1}: a recorded 0 is falsy here — ${line.trim()}`);
          return;
        }
        if (QUANTITY_GREATER_THAN_ONE.test(line)) {
          violations.push(`${rel}:${i + 1}: hides a recorded 0 — ${line.trim()}`);
          return;
        }
        if (!QUANTITY_OR_ONE.test(line)) return;
        const key = `${rel}::${line.trim()}`;
        if (key in ALLOWED) found.add(key);
        else violations.push(`${rel}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(violations).toEqual([]);
    expect(Object.keys(ALLOWED).filter((k) => !found.has(k))).toEqual([]);
  });
});
