// @vitest-environment node
/**
 * Architectural lint — test noise may not hide real errors (GDXA-1068).
 *
 * 1. vitest.config.js once carried `dangerouslyIgnoreUnhandledErrors: true` for
 *    one PrimeVue TabList timer (primefaces/primevue#7410). Being blanket, it
 *    also hid 18 `auth.loadPermissions is not a function` errors thrown by the
 *    Dashboard specs while every test reported green. The config now filters
 *    that one timer by its stack frame; anything else unhandled fails the run.
 *
 * 2. A Playwright init script runs in EVERY frame, including sandboxed and
 *    opaque-origin iframes, where touching sessionStorage/localStorage throws a
 *    SecurityError into the page's error stream. Each init script that writes
 *    storage must return early outside the top frame (`window.top`).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import config, { isPrimeVueInkBarTimerError } from '../../vitest.config.js';

const E2E_DIR = join(__dirname, '..', '..', 'e2e');

/** Every `addInitScript(...)` call in `src`, as its balanced source text. */
function* initScriptCalls(src) {
  const re = /addInitScript\(/g;
  let m;
  while ((m = re.exec(src))) {
    let i = re.lastIndex;
    let depth = 1;
    while (depth && i < src.length) {
      if (src[i] === '(') depth += 1;
      else if (src[i] === ')') depth -= 1;
      i += 1;
    }
    yield { line: src.slice(0, m.index).split('\n').length, text: src.slice(m.index, i) };
  }
}

describe('vitest unhandled errors are filtered, not ignored', () => {
  it('the config does not carry the blanket ignore flag', () => {
    expect(config.test.dangerouslyIgnoreUnhandledErrors).toBeUndefined();
    expect(typeof config.test.onUnhandledError).toBe('function');
  });

  it('drops only the PrimeVue TabList ink-bar timer', () => {
    const inkBar = new TypeError("Cannot read properties of null (reading 'offsetWidth')");
    inkBar.stack = `${inkBar.message}\n    at Proxy.updateInkBar (node_modules/primevue/tablist/index.mjs:73:9)`;
    expect(isPrimeVueInkBarTimerError(inkBar)).toBe(true);
    expect(config.test.onUnhandledError(inkBar)).toBe(false);

    const serialized = {
      message: 'HTMLElement is not defined',
      stacks: [{ method: 'bindInkBarObserver', file: 'node_modules/primevue/tablist/index.mjs' }],
    };
    expect(config.test.onUnhandledError(serialized)).toBe(false);
  });

  it('keeps every other unhandled error, including the one the flag hid', () => {
    const hidden = new TypeError('auth.loadPermissions is not a function');
    hidden.stack = `${hidden.message}\n    at src/components/forecasting/CashCalendarSummaryCard.vue:84:14`;
    expect(config.test.onUnhandledError(hidden)).not.toBe(false);

    const sameMessageElsewhere = new ReferenceError('HTMLElement is not defined');
    sameMessageElsewhere.stack = `${sameMessageElsewhere.message}\n    at src/components/SomethingElse.vue:10:3`;
    expect(config.test.onUnhandledError(sameMessageElsewhere)).not.toBe(false);

    expect(config.test.onUnhandledError({})).not.toBe(false);
  });
});

describe('e2e init scripts write storage only in the top frame', () => {
  it('every addInitScript that touches Storage is guarded by window.top', () => {
    const files = readdirSync(E2E_DIR).filter((n) => n.endsWith('.js'));
    let checked = 0;
    const violations = [];
    for (const name of files) {
      const src = readFileSync(join(E2E_DIR, name), 'utf8');
      for (const call of initScriptCalls(src)) {
        if (!/\b(session|local)Storage\b/.test(call.text)) continue;
        checked += 1;
        // The comparison itself, not a mention of it: a comment does not guard.
        const code = call.text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '');
        if (!/window\s*[!=]==\s*window\.top/.test(code)) violations.push(`e2e/${name}:${call.line}`);
      }
    }
    // The scan must be looking at something: 85 storage init scripts on 2026-10-14.
    expect(checked).toBeGreaterThan(50);
    expect(violations).toEqual([]);
  });
});
