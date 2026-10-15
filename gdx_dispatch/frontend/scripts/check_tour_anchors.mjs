#!/usr/bin/env node
// CI gate: every tour step's anchor selector must resolve to a
// `data-tour="<value>"` attribute somewhere in the Vue source.
//
// Stops the class of bug where a Vue refactor renames or removes an
// element that a tour step targeted — the tour silently filters that
// step out at runtime, so the user sees half a tour and we never know.
//
// Wired into `npm run build` via `prebuild`.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const CATALOG_PATH = join(ROOT, 'src/tours/catalog.js');
const SRC_DIR = join(ROOT, 'src');

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (entry === 'node_modules' || entry === '__tests__' || entry === 'help') continue;
      out.push(...walk(full));
    } else if (entry.endsWith('.vue') || entry.endsWith('.js') || entry.endsWith('.ts')) {
      out.push(full);
    }
  }
  return out;
}

function extractTourAnchors(catalogSource) {
  const re = /anchor:\s*['"`]\[data-tour=['"]([^'"]+)['"]\]['"`]/g;
  const anchors = new Set();
  let m;
  while ((m = re.exec(catalogSource)) !== null) {
    anchors.add(m[1]);
  }
  return anchors;
}

function findAnchorOccurrences(files) {
  // Match both static (data-tour="foo") and bound (:data-tour="`nav-${x}`")
  // forms. Bound forms expand to many possible values, so we extract the
  // literal prefix/suffix and treat the variable interior as wildcards
  // resolved at runtime — for the CI gate, we surface them as "covered
  // by a template binding" and accept any anchor that matches the
  // prefix-with-anything-suffix pattern.
  const staticAnchors = new Set();
  const templatePatterns = []; // [{ prefix, suffix }]
  const staticRe = /\bdata-tour=['"]([^'"]+)['"]/g;
  const boundRe = /:\s*data-tour=['"]`([^`]*)`['"]/g;
  for (const f of files) {
    const text = readFileSync(f, 'utf8');
    let m;
    while ((m = staticRe.exec(text)) !== null) staticAnchors.add(m[1]);
    while ((m = boundRe.exec(text)) !== null) {
      // m[1] looks like `nav-${(m.to || '').replace(/^\//, '').replace(/\//g, '-')}`
      const tpl = m[1];
      const parts = tpl.split(/\$\{[^}]*\}/);
      if (parts.length >= 2) {
        templatePatterns.push({ prefix: parts[0], suffix: parts[parts.length - 1] });
      }
    }
  }
  return { staticAnchors, templatePatterns };
}

function anchorCovered(needle, staticAnchors, templatePatterns) {
  if (staticAnchors.has(needle)) return true;
  for (const tpl of templatePatterns) {
    if (needle.startsWith(tpl.prefix) && needle.endsWith(tpl.suffix)) return true;
  }
  return false;
}

function main() {
  const catalogSource = readFileSync(CATALOG_PATH, 'utf8');
  const required = extractTourAnchors(catalogSource);
  if (required.size === 0) {
    console.log('[tour-anchors] catalog has no anchor references — nothing to check');
    return 0;
  }
  const files = walk(SRC_DIR);
  const { staticAnchors, templatePatterns } = findAnchorOccurrences(files);

  const missing = [];
  for (const a of required) {
    if (!anchorCovered(a, staticAnchors, templatePatterns)) {
      missing.push(a);
    }
  }

  if (missing.length) {
    console.error('[tour-anchors] BUILD FAILED — these tour anchors are referenced but no element carries them:');
    for (const a of missing) console.error('  - ' + a);
    console.error('Fix by either:');
    console.error('  1. Adding `data-tour="<id>"` to the right element, OR');
    console.error('  2. Removing the step from src/tours/catalog.js if it\'s obsolete.');
    process.exit(1);
  }
  console.log(`[tour-anchors] ✓ ${required.size} tour anchor${required.size === 1 ? '' : 's'} all resolve`);
  return 0;
}

main();
