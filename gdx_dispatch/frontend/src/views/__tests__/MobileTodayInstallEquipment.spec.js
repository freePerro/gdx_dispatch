/**
 * MobileTodayView — Install & equipment section (Doug 2026-06-29).
 *
 * The tech's job card gains a read-only "Install & equipment" panel that lazy-
 * loads the customer's installed equipment (door + opener specs) from the
 * existing GET /api/customers/{id}/equipment endpoint, so a tech on an install
 * or service call sees the unit details (model, serial, install date, warranty,
 * spec notes) without leaving the route.
 *
 * Static-source spec (same style as MobileTodayCloseoutWiring) — pins the
 * wiring so a refactor can't silently drop it.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'MobileTodayView.vue'), 'utf8');

describe('MobileTodayView — install & equipment section', () => {
  it('lazy-loads equipment from the customer-scoped endpoint', () => {
    expect(SRC).toMatch(/api\.get\(`\/api\/customers\/\$\{customerId\}\/equipment`\)/);
  });

  it('defines toggleEquipment + loadEquipment', () => {
    expect(SRC).toMatch(/async function toggleEquipment\(/);
    expect(SRC).toMatch(/async function loadEquipment\(/);
  });

  it('fails quietly when the equipment module is off (no toast)', () => {
    // The panel is supplementary — a 403/404 (module disabled) must degrade to
    // "none on file", never an error toast that scares the tech.
    const fnStart = SRC.indexOf('async function loadEquipment');
    const fnSpan = SRC.slice(fnStart, fnStart + 600);
    expect(fnSpan).toMatch(/catch\s*\{/);
    expect(fnSpan).not.toMatch(/toast\.add/);
  });

  it('renders the equipment row + panel keyed by customer id', () => {
    expect(SRC).toMatch(/@click="toggleEquipment\(job\)"/);
    expect(SRC).toMatch(/equipByCustomer\[job\.customer\?\.id\]/);
    expect(SRC).toMatch(/data-testid="`equipment-panel-\$\{job\.id\}`"/);
  });

  it('labels the door equipment type', () => {
    expect(SRC).toMatch(/garage_door:\s*'Garage door'/);
  });
});
