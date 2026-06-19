/**
 * Architectural lint — only the auth store and the platform-login
 * handoff scrubber may write `gdx_access_token` to sessionStorage.
 *
 * 2026-05-09 incident: a stale `gdx_access_token` from a prior session
 * left the route guard's `isAuthenticated = Boolean(accessToken.value)`
 * stuck at `true`, so a wrong-password login returned "Invalid
 * credentials" but /dashboard still rendered as the prior admin. The
 * unit-test fix in `auth.test.js` proves the chokepoint clears on
 * failure — but that's necessary, not sufficient. If a future feature
 * smuggles a token write through some sneaky path (a composable, a
 * router guard, an event handler), the chokepoint never sees it and
 * the whole defense bypasses.
 *
 * This test pins the structural invariant: only two source files in
 * src/ may call `sessionStorage.setItem('gdx_access_token', ...)`:
 *   - src/stores/auth.js (login() + refreshAccessToken())
 *   - src/main.js        (platform-login handoff fragment scrubber)
 *
 * If a third source surfaces, this test fails. Either the writer
 * belongs in the auth store (most likely) or it joins the allowlist
 * here with a documented reason.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SRC_DIR = join(__dirname, '..');

const ALLOWED = new Set([
  'stores/auth.js',
  'main.js',
]);

// Pattern: sessionStorage.setItem('gdx_access_token', ...) — accepts any quote
// style and any whitespace, but the FIRST argument must be the token key
// literal. Bracket access (`sessionStorage['gdx_access_token'] = …`) is
// caught by the second pattern.
const SETITEM_RE = /sessionStorage\.setItem\(\s*['"]gdx_access_token['"]/;
const BRACKET_RE = /sessionStorage\[\s*['"]gdx_access_token['"]\s*\]\s*=/;

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      // Skip test directories — fixtures setting up state in a test env
      // are not a security risk and would otherwise fail this lint.
      if (name === '__tests__') continue;
      yield* walk(full);
    } else if (/\.(js|mjs|ts|vue)$/.test(name)) {
      yield full;
    }
  }
}

function relSrc(file) {
  return file.replace(SRC_DIR + '/', '');
}

describe('architectural lint — sessionStorage token writes', () => {
  it('only the allowlisted files may write gdx_access_token', () => {
    const violations = [];
    for (const file of walk(SRC_DIR)) {
      const src = readFileSync(file, 'utf8');
      if (SETITEM_RE.test(src) || BRACKET_RE.test(src)) {
        const rel = relSrc(file);
        if (!ALLOWED.has(rel)) {
          violations.push(rel);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('the allowlisted files actually do write the token (sanity check)', () => {
    // If this fails it means we deleted the legitimate path; the
    // allowlist may need to shrink.
    for (const allowedRel of ALLOWED) {
      const src = readFileSync(join(SRC_DIR, allowedRel), 'utf8');
      const found = SETITEM_RE.test(src) || BRACKET_RE.test(src);
      expect(found, `${allowedRel} should write gdx_access_token`).toBe(true);
    }
  });
});
