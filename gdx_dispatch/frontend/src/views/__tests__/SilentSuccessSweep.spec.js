/**
 * Silent-success sweep (2026-09-19) — pins that the removed lies stay removed.
 *
 * Per the repo's own rule, a test asserting source text is PRESENT proves
 * nothing, but asserting text is ABSENT is a real guard: each block below
 * red-flags the exact string that made a surface fake success.
 *
 *  - CollectionsView: "Export CSV" used to window.open /api/collections/export
 *    (a route that has never existed) and toast "Export started".
 *  - TimeclockView: "Submit Day" used to set todaySubmitted BEFORE the request
 *    and swallow every error ("Endpoint may not exist yet").
 *  - SurveysView: the send button used to toast "Survey sent" over an endpoint
 *    that mints a link and sends nothing.
 *  - router/modules: the Admin Operations page (4 fabricated-success buttons,
 *    hardcoded-empty list) is deleted.
 */
import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const read = (rel) => readFileSync(join(__dirname, '..', rel), 'utf8');

describe('silent-success sweep — removed lies stay removed', () => {
  it('CollectionsView no longer opens the nonexistent export route', () => {
    const src = read('CollectionsView.vue');
    expect(src).not.toContain('/api/collections/export');
    expect(src).not.toContain('Your CSV will download shortly');
  });

  it('TimeclockView no longer fakes the submit before/despite the request', () => {
    const src = read('TimeclockView.vue');
    expect(src).not.toContain('Endpoint may not exist yet');
    const fn = src.slice(src.indexOf('async function confirmSubmitDay'));
    const post = fn.indexOf("api.post('/api/timeclock/submit-day'");
    const mark = fn.indexOf('todaySubmitted.value = true');
    expect(post).toBeGreaterThan(-1);
    expect(mark).toBeGreaterThan(post);
  });

  it('SurveysView no longer claims a send that never happens', () => {
    const src = read('SurveysView.vue');
    expect(src).not.toContain('Survey sent');
  });

  it('the Admin Operations page is gone from view, router and nav', () => {
    expect(existsSync(join(__dirname, '..', 'AdminOpsView.vue'))).toBe(false);
    const router = readFileSync(join(__dirname, '..', '..', 'router', 'index.js'), 'utf8');
    expect(router).not.toContain('AdminOpsView');
    const modules = readFileSync(join(__dirname, '..', '..', 'constants', 'modules.js'), 'utf8');
    expect(modules).not.toMatch(/to: '\/admin-ops'/);
  });
});
