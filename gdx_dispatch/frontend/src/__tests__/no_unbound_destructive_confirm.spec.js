/**
 * Architectural lint — a file that calls confirmAsync() or confirmDestructive()
 * must bind it from useDestructiveConfirm (GDXA-1688).
 *
 * SurveysView, CustomFieldsView and admin/TechMobileSettingsView each called
 * `confirmAsync(` with no import and no binding. There is no auto-import and
 * no global, so the Delete click threw a ReferenceError and did nothing — and
 * no build step, test or lint noticed, because the name only resolves when
 * the handler runs. This scan fails when a file calls either function without
 * importing the composable and destructuring the name from it.
 *
 * Excluded: __tests__/ (specs mock the composable), and the composable itself.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');
const COMPOSABLE = join(SRC_DIR, 'composables', 'useDestructiveConfirm.js');
const NAMES = ['confirmAsync', 'confirmDestructive'];

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name === '__tests__' || name === 'node_modules') continue;
      yield* walk(full);
    } else if (/\.(vue|js|ts)$/.test(name) && !/\.spec\.[jt]s$/.test(name)) {
      yield full;
    }
  }
}

// Drop HTML comments and comment-only lines. Not a JS parser on purpose: a
// regex over `/* ... */` also matches inside strings such as accept="image/*"
// and swallowed real bindings. HTML comments are cut by index, not by a regex
// replace, so no `<!--` survives a single pass (CodeQL
// js/incomplete-multi-character-sanitization). An unterminated `<!--` is kept,
// so a scan never hides code after it.
function stripHtmlComments(src) {
  let out = '';
  let i = 0;
  for (;;) {
    const start = src.indexOf('<!--', i);
    if (start === -1) return out + src.slice(i);
    const end = src.indexOf('-->', start + 4);
    if (end === -1) return out + src.slice(i);
    out += src.slice(i, start);
    i = end + 3;
  }
}

function stripComments(src) {
  return stripHtmlComments(src)
    .split('\n')
    .filter((line) => !/^\s*(\/\/|\/\*|\*)/.test(line))
    .join('\n');
}

export function unboundCalls(src) {
  const code = stripComments(src);
  const imported = /import\s*\{[^}]*\buseDestructiveConfirm\b[^}]*\}\s*from\s*['"][^'"]*useDestructiveConfirm(?:\.js)?['"]/.test(code);
  const missing = [];
  for (const name of NAMES) {
    if (!new RegExp(`(?<![.\\w])${name}\\s*\\(`).test(code)) continue;
    const bound = new RegExp(`\\{[^}]*\\b${name}\\b[^}]*\\}\\s*=\\s*useDestructiveConfirm\\s*\\(`).test(code);
    if (!imported || !bound) missing.push(name);
  }
  return missing;
}

describe('architectural lint — destructive confirms are bound before they are called', () => {
  it('every caller imports useDestructiveConfirm and destructures the name it calls', () => {
    const violations = [];
    for (const file of walk(SRC_DIR)) {
      if (file === COMPOSABLE) continue;
      const missing = unboundCalls(readFileSync(file, 'utf8'));
      if (missing.length) violations.push(`${relative(SRC_DIR, file)}: ${missing.join(', ')}`);
    }
    expect(violations).toEqual([]);
  });

  it('the scan catches the GDXA-1688 shape and passes the fixed one', () => {
    const broken = `<script setup>
import { useApiWithToast } from '../composables/useApiWithToast';
async function del() { if (!(await confirmAsync({ message: 'x' }))) return; }
</script>`;
    const fixed = `<script setup>
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();
async function del() { if (!(await confirmAsync({ message: 'x' }))) return; }
</script>`;
    const importOnly = `<script setup>
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmDestructive } = useDestructiveConfirm();
async function del() { await confirmAsync({ message: 'x' }); }
</script>`;
    expect(unboundCalls(broken)).toEqual(['confirmAsync']);
    expect(unboundCalls(fixed)).toEqual([]);
    expect(unboundCalls(importOnly)).toEqual(['confirmAsync']);
  });

  it('ignores calls inside HTML comments but not after an unterminated one', () => {
    const commented = `<template><!-- <Button @click="confirmAsync({})" /> --></template>`;
    const unterminated = `<template><!-- <Button @click="confirmAsync({})" /></template>`;
    expect(unboundCalls(commented)).toEqual([]);
    expect(unboundCalls(unterminated)).toEqual(['confirmAsync']);
  });
});
