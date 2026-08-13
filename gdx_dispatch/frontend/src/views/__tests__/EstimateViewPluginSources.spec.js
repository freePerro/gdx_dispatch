/**
 * EstimateView — multi-provider estimate_source buttons + finalized line lock
 * (2026-08-13, docs/design/estimate-screen-plugin-pricing-plan.md Phase 1).
 *
 * Source-pin style (EstimateView has no mount harness): the discovery /
 * filtering logic is mount-tested in composables/__tests__/
 * useEstimateSources.spec.js; these pins hold the view wiring in place.
 *
 * Two behaviors worth pinning hard:
 *  - one "Add {label}" button PER provider (the old `.find()` made any second
 *    estimate_source plugin unreachable from the estimate screen);
 *  - the line-item editor locks on accepted/declined estimates. Autosave
 *    silently refuses to run for those (FINALIZED gate in _flushNow), so an
 *    editable toolbar there ate every added line — rendered, never persisted,
 *    vanished on reload.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'EstimateView.vue'), 'utf8');

describe('EstimateView — multi-provider estimate_source wiring', () => {
  it('discovers providers through the shared composable, not an inline .find()', () => {
    expect(SRC).toMatch(/import \{ classifyPickerError, useEstimateSources \} from "\.\.\/composables\/useEstimateSources"/);
    expect(SRC).toMatch(/useEstimateSources\(api, auth\)/);
    expect(SRC).not.toMatch(/\.find\(\(x\) => x\?\.ui\?\.estimate_source\)/);
  });

  it('renders one button per provider and keeps the legacy testid for a single provider', () => {
    expect(SRC).toMatch(/v-for="src in estimateSources" :key="src\.pluginKey"/);
    expect(SRC).toMatch(/openCapturedPicker\(src\)/);
    expect(SRC).toMatch(/\? 'est-add-captured-btn' : `est-add-captured-btn-\$\{src\.pluginKey\}`/);
  });

  it('opens the picker against the invoked provider (activeSource), not a singleton', () => {
    expect(SRC).toMatch(/activeSource\.value = source \|\| estimateSources\.value\[0\] \|\| null/);
    expect(SRC).toMatch(/activeSource\.value\.list_endpoint, \{ suppressErrorToast: true \}/);
    expect(SRC).toMatch(/activeSource\.value\.draft_endpoint\.replace\("\{id\}", item\.id\)/);
  });

  it('renders provider-declared picker columns instead of the legacy hardcoded fields', () => {
    expect(SRC).toMatch(/v-for="c in activeSource\?\.columns \|\| \[\]"/);
    expect(SRC).not.toMatch(/<Column field="qcd" header="Quote #" \/>/);
    // currency(undefined) prints "$0.00" — a missing money field must render
    // empty, not a fabricated zero price (caught in the local browser walk).
    expect(SRC).toMatch(/c\.money \? \(data\[c\.field\] == null \? "" : currency\(data\[c\.field\]\)\)/);
  });

  it('parses valid_until/expires_at as date-only so UTC midnight does not display a day early', () => {
    expect(SRC).toMatch(/function _parseDateOnly\(v\)/);
    expect(SRC).toMatch(/valid_until: _parseDateOnly\(data\.valid_until \|\| data\.expires_at\) \|\| defaultValidUntil\(\)/);
    expect(SRC).toMatch(/expires_at: _parseDateOnly\(/);
  });

  it('distinguishes forbidden / unavailable / empty in the picker instead of a silent empty list', () => {
    expect(SRC).toMatch(/data-testid="captured-picker-error"/);
    expect(SRC).toMatch(/capturedError === 'forbidden'/);
    expect(SRC).toMatch(/classifyPickerError\(e\)/);
    // The empty state must gate on the ITEMS — capturedFolderList always seeds
    // an "All items" row, so gating on it renders the empty state never.
    expect(SRC).toMatch(/<p v-if="!capturedItems\.length" class="captured-hint" data-testid="captured-picker-empty">/);
  });

  it('treats a picker 401 as session expiry, not a plugin failure', () => {
    // suppressErrorToast skips the global session-expiry handler, so the
    // picker handles the 401 itself instead of showing "plugin not responding".
    const catchBlock = SRC.slice(SRC.indexOf('async function openCapturedPicker'), SRC.indexOf('function openFolder'));
    expect(catchBlock).toMatch(/e\?\.status === 401/);
    expect(catchBlock).toMatch(/auth\.logout\(\)/);
    expect(catchBlock).toMatch(/router\.push\("\/login"\)/);
  });

  it('drops picker rows without an id (dataKey + draft URL both need it)', () => {
    expect(SRC).toMatch(/\.filter\(\(r\) => r && r\.id != null\)/);
  });

  it('warns when an added line is missing the description or price autosave requires', () => {
    expect(SRC).toMatch(/if \(!\(li\.description && Number\(li\.unit_price\) > 0\)\) unpriced \+= 1;/);
    expect(SRC).toMatch(/needs a description and a Unit Price before it saves/);
  });

  it('names attached photos by provider key, not a hardcoded prefix', () => {
    expect(SRC).toMatch(/\$\{activeSource\.value\?\.pluginKey \|\| "plugin"\}-\$\{item\.qcd \|\| item\.id\}/);
  });
});

describe('EstimateView — finalized estimates lock the whole editor', () => {
  it('derives the lock from the same FINALIZED set autosave uses', () => {
    expect(SRC).toMatch(/const estimateLocked = computed\(\(\) => isExisting\.value && FINALIZED\.has\(estimate\.value\.status\)\)/);
  });

  it('replaces the add-line toolbar with a lock notice when finalized', () => {
    expect(SRC).toMatch(/<div v-if="!estimateLocked" class="line-item-buttons">/);
    expect(SRC).toMatch(/data-testid="est-lines-locked"/);
  });

  it('disables every row control that would silently not save', () => {
    // delete + both reorder buttons + the 5 editable fields
    expect(SRC).toMatch(/:disabled="estimateLocked" @click="removeLineAt\(idx\)"/);
    expect(SRC).toMatch(/:disabled="estimateLocked \|\| idx === 0"/);
    expect(SRC).toMatch(/:disabled="estimateLocked \|\| idx === form\.line_items\.length - 1"/);
    const disabledFields = SRC.match(/:data-testid="`est-line-(cat|desc|qty|cost|price|margin)-\$\{idx\}`" :disabled="estimateLocked"/g) || [];
    expect(disabledFields.length).toBeGreaterThanOrEqual(5);
  });

  it('locks the header fields too — autosave skips the ENTIRE flush, not just lines', () => {
    // _flushNow bails wholesale for finalized estimates, so every editable
    // header field was the same silent-loss defect one panel up.
    // This list is built from _flushNow's PATCH payload (label, valid_until,
    // jobsite_address, description, notes, tax_rate, discount,
    // hide_line_prices) — every autosaved header field, not just the ones that
    // happened to get fixed. The re-audit caught hide_line_prices missing
    // precisely because the first version of this list was fix-derived.
    for (const id of ['est-label', 'est-valid-until', 'est-jobsite', 'est-description',
      'est-tax-rate', 'est-discount', 'est-notes', 'est-hide-line-prices']) {
      const at = SRC.indexOf(`id="${id}"`);
      expect(at, `${id} missing`).toBeGreaterThan(-1);
      const tag = SRC.slice(at, SRC.indexOf('/>', at));
      expect(tag, `${id} not locked`).toMatch(/:disabled="estimateLocked"/);
    }
  });

  it('disables Save Changes and the jobsite-copy button when finalized', () => {
    // forceFlush refuses to run then — the "Saved" toast would be a lie.
    const save = SRC.slice(SRC.indexOf('label="Save Changes"'), SRC.indexOf('@click="saveExistingEstimate"'));
    expect(save).toMatch(/:disabled="estimateLocked"/);
    const copy = SRC.slice(SRC.indexOf('label="Use as jobsite"'), SRC.indexOf('copy-customer-address-to-jobsite'));
    expect(copy).toMatch(/:disabled="estimateLocked"/);
  });
});
