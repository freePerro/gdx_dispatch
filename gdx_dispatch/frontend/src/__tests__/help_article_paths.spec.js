/**
 * Help-article lint — every "X → Y" path a help article tells the reader to
 * follow must land on a real place.
 *
 * GDXA-668/669 (2026-10-11): the invoices article sent admins to
 * "Settings → Reminders", which never existed (reminders are the Billing hub's
 * Reminders tab), and a sweep found nine more invented Settings sections
 * across the help corpus (Customer comms, Workflow gates, Role permissions,
 * Security, API tokens, Support, Company, Dispatch). Prose can't be checked
 * for truth, but a navigation path can be checked for existence.
 *
 * Roots and their children:
 *   - Settings          → the <Tab> labels in SettingsView.vue
 *   - a sidebar category (Admin, Customers, …) → that category's module labels
 *   - a hub (Billing, Phone, …) → the tabLabels of the modules in that cluster
 * Only the first step after a known root is checked; deeper steps (a card
 * heading, a button) are left to the author. Every "→" on a line is checked
 * when the word before it is a known root (bold or not) and the step after it
 * starts with a capital; "settings > x", an unknown first word
 * ("Arrived → Complete") and a lowercase step ("**Admin** → everything", a
 * role list) are not checked.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { MODULE_CATEGORIES, NAV_CLUSTERS, flattenModules } from '../constants/modules.js';

const ARTICLES_DIR = join(__dirname, '..', 'help', 'articles');
const SETTINGS_VIEW = join(__dirname, '..', 'views', 'SettingsView.vue');

function buildRoots() {
  const roots = new Map();
  const add = (root, labels) => {
    const set = roots.get(root) || new Set();
    labels.filter(Boolean).forEach((l) => set.add(l));
    roots.set(root, set);
  };
  const settingsSrc = readFileSync(SETTINGS_VIEW, 'utf8');
  add('Settings', [...settingsSrc.matchAll(/<Tab value="[^"]+">([^<]+)<\/Tab>/g)].map((m) => m[1].trim()));
  for (const cat of MODULE_CATEGORIES) add(cat.label, cat.modules.map((m) => m.label));
  const all = flattenModules();
  for (const hub of NAV_CLUSTERS) {
    add(hub.label, all.filter((m) => m.cluster === hub.key).map((m) => m.tabLabel || m.label));
  }
  return roots;
}

function findPaths(text, roots) {
  const labels = new Set([...roots.values()].flatMap((set) => [...set]));
  const out = [];
  for (const [i, line] of text.split('\n').entries()) {
    for (const m of line.matchAll(/([A-Z][A-Za-z]*)(?:\*\*)? → (?=(?:\*\*)?([^a-z\s][^\n]*))/g)) {
      const [, root, rest] = m;
      if (!roots.has(root)) continue;
      // "Settings → Feature Settings → Job Workflow": the second "Settings"
      // is the tail of a tab label, not a new root.
      const before = line.slice(0, m.index).match(/((?:[A-Z][A-Za-z&]* )+)$/);
      if (before && labels.has(before[1] + root)) continue;
      out.push({ line: i + 1, root, rest });
    }
  }
  return out;
}

function landsOnChild(rest, children) {
  return [...children].some((label) => {
    if (!rest.startsWith(label)) return false;
    const next = rest.charAt(label.length);
    return next === '' || !/[A-Za-z0-9]/.test(next);
  });
}

describe('help article navigation paths', () => {
  const roots = buildRoots();

  it('found the Settings tabs and the sidebar to check against', () => {
    for (const tab of ['Branding', 'Users', 'Integrations', 'Modules']) {
      expect(roots.get('Settings').has(tab)).toBe(true);
    }
    expect(roots.get('Billing').has('Reminders')).toBe(true);
    expect(roots.get('Admin').has('Roles & Permissions')).toBe(true);
  });

  it('rejects a path to a place that does not exist', () => {
    const bad = findPaths('Set it under Settings → Reminders.', roots);
    expect(bad).toHaveLength(1);
    expect(landsOnChild(bad[0].rest, roots.get(bad[0].root))).toBe(false);
    for (const text of ['Open **Settings** → **Security**.', 'Open **Settings** → Security.']) {
      const bold = findPaths(text, roots);
      expect(bold).toHaveLength(1);
      expect(landsOnChild(bold[0].rest, roots.get(bold[0].root))).toBe(false);
    }
    const two = findPaths('Use Settings → Modules or Settings → Bogus.', roots);
    expect(two.map((p) => landsOnChild(p.rest, roots.get(p.root)))).toEqual([true, false]);
    expect(findPaths('- **Admin** → everything', roots)).toEqual([]);
    expect(findPaths('Settings → Feature Settings → Job Workflow', roots)).toHaveLength(1);
  });

  it('every "Root → Place" in a help article names a real place', () => {
    const broken = [];
    for (const name of readdirSync(ARTICLES_DIR).filter((n) => n.endsWith('.md'))) {
      const text = readFileSync(join(ARTICLES_DIR, name), 'utf8');
      for (const p of findPaths(text, roots)) {
        if (!landsOnChild(p.rest, roots.get(p.root))) {
          broken.push(`${name}:${p.line}  ${p.root} → ${p.rest.slice(0, 40)}`);
        }
      }
    }
    expect(broken).toEqual([]);
  });
});
