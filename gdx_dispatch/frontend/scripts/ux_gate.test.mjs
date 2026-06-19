#!/usr/bin/env node
/**
 * Negative-test for ux_gate.mjs — assert each rule fires on synthetic
 * violations. Without this, a regex drift in the gate could silently let
 * the bug class back in (Gemma 2026-05-09 improvement suggestion from S110c
 * stanza). Runs via `node scripts/ux_gate.test.mjs` — no external test
 * framework needed; exits 0 on all-pass, 1 on any failure.
 *
 * Strategy: write a temp .vue file containing each banned pattern, run
 * the gate against just that file's directory, assert the gate exits
 * with code 1 AND the failure message mentions the offending rule.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execSync } from 'node:child_process';

const ROOT = path.resolve(new URL('..', import.meta.url).pathname);
const GATE = path.join(ROOT, 'scripts', 'ux_gate.mjs');
const SCRATCH = fs.mkdtempSync(path.join(os.tmpdir(), 'ux-gate-test-'));

function writeFixture(filename, content) {
  // Pre-create BOTH scan dirs so the gate's `find` doesn't error on a
  // missing directory. Empty `components` is fine — the gate just scans it.
  const viewsDir = path.join(SCRATCH, 'src', 'views');
  const componentsDir = path.join(SCRATCH, 'src', 'components');
  fs.mkdirSync(viewsDir, { recursive: true });
  fs.mkdirSync(componentsDir, { recursive: true });
  const fp = path.join(viewsDir, filename);
  fs.writeFileSync(fp, content);
  return fp;
}

function runGate(scanDir) {
  // Point the gate at our scratch directory via UX_GATE_ROOT so it scans
  // ONLY our fixture (mirrored to scratchDir/src/views/...). Without the
  // override the gate scans the real repo and our fixture is invisible.
  try {
    execSync(`node ${GATE}`, {
      cwd: scanDir,
      encoding: 'utf8',
      stdio: 'pipe',
      env: { ...process.env, UX_GATE_ROOT: scanDir },
    });
    return { code: 0, out: '' };
  } catch (e) {
    return { code: e.status, out: (e.stdout || '') + (e.stderr || '') };
  }
}

const cases = [
  {
    name: 'severity="warning" (banned v3 vocab)',
    file: 'TestSeverityWarning.vue',
    body: `<template><div><Toast severity="warning" /></div></template>`,
    expectMatch: /severity="warning"/,
  },
  {
    name: 'severity="error" on Tag (must be danger)',
    file: 'TestSeverityErrorOnTag.vue',
    body: `<template><div><Tag severity="error" /></div></template>`,
    expectMatch: /severity="error" on Tag/,
  },
  {
    name: 'severity="danger" on Toast (must be error)',
    file: 'TestSeverityDangerOnToast.vue',
    body: `<template><div><Toast severity="danger" /></div></template>`,
    expectMatch: /severity="danger" on Toast/,
  },
  {
    name: 'native confirm()',
    file: 'TestNativeConfirm.vue',
    body: `<template><div /></template><script setup>function go() { if (!confirm('really?')) return; }</script>`,
    expectMatch: /native confirm\(\)/,
  },
  {
    name: 'unregistered PrimeVue component',
    file: 'TestUnregisteredComponent.vue',
    body: `<template><div><Calendar v-model="x" /></div></template>
<script setup>
import { ref } from 'vue';
const x = ref(null);
</script>`,
    expectMatch: /unregistered PrimeVue component/,
  },
];

let pass = 0;
let fail = 0;
for (const c of cases) {
  // Write fixture into our temp scan dir
  fs.rmSync(path.join(SCRATCH, 'src'), { recursive: true, force: true });
  writeFixture(c.file, c.body);
  const result = runGate(SCRATCH);
  if (result.code !== 1) {
    console.log(`✗ ${c.name}: expected exit 1, got ${result.code}`);
    fail++;
    continue;
  }
  if (!c.expectMatch.test(result.out)) {
    console.log(`✗ ${c.name}: output didn't match ${c.expectMatch}`);
    console.log(`  got: ${result.out.slice(0, 400)}`);
    fail++;
    continue;
  }
  console.log(`✓ ${c.name}`);
  pass++;
}

// Cleanup
fs.rmSync(SCRATCH, { recursive: true, force: true });

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
