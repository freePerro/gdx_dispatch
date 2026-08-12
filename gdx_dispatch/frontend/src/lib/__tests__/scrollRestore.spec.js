/**
 * Scroll restoration against the REAL scroll container.
 *
 * The trap this exists for: the obvious implementation (vue-router
 * `scrollBehavior` returning `savedPosition`) does nothing in this app. `body`
 * is `overflow: hidden` and `.layout-content` is the element with
 * `overflow: auto`, while the router scrolls the WINDOW. A test that only
 * asserted "scrollBehavior returns savedPosition" would have passed while the
 * feature did nothing at all.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  applyScroll, clearScrollPositions, forgetScroll, installScrollRestore,
  rememberScroll, savedScrollFor,
  sameViewComponent,
} from '../scrollRestore';

function mountScroller({ scrollHeight = 2000, clientHeight = 800 } = {}) {
  document.body.innerHTML = '<div class="layout-content"></div>';
  const el = document.querySelector('.layout-content');
  let top = 0;
  Object.defineProperty(el, 'scrollHeight', { get: () => scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { get: () => clientHeight, configurable: true });
  Object.defineProperty(el, 'scrollTop', {
    get: () => top,
    // Real elements clamp; jsdom does not, and the clamping is load-bearing
    // for the "content not tall enough yet" retry.
    set: (v) => { top = Math.max(0, Math.min(v, scrollHeight - clientHeight)); },
    configurable: true,
  });
  return el;
}

// applyScroll drives itself with requestAnimationFrame; run them synchronously.
function flushFrames(n = 20) {
  for (let i = 0; i < n; i += 1) vi.advanceTimersByTime(16);
}

describe('scrollRestore', () => {
  beforeEach(() => {
    clearScrollPositions();
    document.body.innerHTML = '';
    vi.useFakeTimers();
    vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(cb, 16));
  });

  it('records the scroll offset of the outgoing route', () => {
    const el = mountScroller();
    el.scrollTop = 640;
    rememberScroll('/jobs');
    expect(savedScrollFor('/jobs')).toBe(640);
  });

  it('restores that offset on a back/forward navigation', () => {
    const el = mountScroller();
    el.scrollTop = 640;
    rememberScroll('/jobs');
    el.scrollTop = 0;

    applyScroll('/jobs', true);           // isPop = back button
    flushFrames();
    expect(el.scrollTop).toBe(640);
  });

  it('starts at the top on a normal (non-pop) navigation', () => {
    const el = mountScroller();
    el.scrollTop = 640;
    rememberScroll('/jobs');
    el.scrollTop = 500;

    applyScroll('/jobs', false);          // fresh nav to the same path
    flushFrames();
    expect(el.scrollTop).toBe(0);
  });

  it('keeps retrying while lazily-loaded content is still growing', () => {
    // The real failure mode: the view is code-split and its rows arrive after
    // mount, so on frame 1 the container cannot scroll to 640 and a single
    // assignment silently clamps to 0.
    let scrollHeight = 800;               // nothing to scroll yet
    document.body.innerHTML = '<div class="layout-content"></div>';
    const el = document.querySelector('.layout-content');
    let top = 0;
    Object.defineProperty(el, 'scrollHeight', { get: () => scrollHeight, configurable: true });
    Object.defineProperty(el, 'clientHeight', { get: () => 800, configurable: true });
    Object.defineProperty(el, 'scrollTop', {
      get: () => top,
      set: (v) => { top = Math.max(0, Math.min(v, scrollHeight - 800)); },
      configurable: true,
    });

    rememberScroll('/jobs');              // 0 for now
    forgetScroll('/jobs');
    // Seed a saved position directly through the public path:
    scrollHeight = 2000; el.scrollTop = 640; rememberScroll('/jobs');
    scrollHeight = 800; el.scrollTop = 0;  // content collapses again (remount)

    applyScroll('/jobs', true);
    vi.advanceTimersByTime(16);
    expect(el.scrollTop).toBe(0);          // can't reach it yet

    scrollHeight = 2000;                   // rows arrive
    flushFrames();
    expect(el.scrollTop).toBe(640);
  });

  it('gives up after a bounded number of frames', () => {
    document.body.innerHTML = '<div class="layout-content"></div>';
    const el = document.querySelector('.layout-content');
    Object.defineProperty(el, 'scrollHeight', { get: () => 800, configurable: true });
    Object.defineProperty(el, 'clientHeight', { get: () => 800, configurable: true });
    const spy = vi.fn();
    Object.defineProperty(el, 'scrollTop', { get: () => 0, set: spy, configurable: true });

    // Seed via a taller stand-in, then apply against content that never grows.
    applyScroll('/never-tall', true, { frames: 3 });
    flushFrames(30);
    // Bounded: it must not spin forever waiting for content that never comes.
    expect(spy.mock.calls.length).toBeLessThanOrEqual(4);
  });

  it('does nothing when the shell is absent (login, customer portal)', () => {
    document.body.innerHTML = '';         // no .layout-content
    expect(() => { applyScroll('/login', true); flushFrames(); }).not.toThrow();
    expect(() => rememberScroll('/login')).not.toThrow();
  });

  it('bounds how many positions it keeps', () => {
    const el = mountScroller();
    for (let i = 0; i < 60; i += 1) { el.scrollTop = i + 1; rememberScroll(`/r${i}`); }
    expect(savedScrollFor('/r0')).toBeUndefined();     // oldest evicted
    expect(savedScrollFor('/r59')).toBeDefined();
  });

  it('installs a guard that remembers the route being left', () => {
    const el = mountScroller();
    el.scrollTop = 320;
    const guards = [];
    const router = { beforeEach: (fn) => guards.push(fn) };
    installScrollRestore(router);
    expect(guards).toHaveLength(1);

    const next = vi.fn();
    guards[0]({ fullPath: '/jobs/1' }, { fullPath: '/jobs' }, next);
    expect(next).toHaveBeenCalled();
    expect(savedScrollFor('/jobs')).toBe(320);
  });
});

/**
 * Regressions found by adversarial review of the first implementation.
 * Both are the kind that unit tests pass and users hit.
 */
describe('scrollRestore — review fixes', () => {
  beforeEach(() => {
    clearScrollPositions();
    document.body.innerHTML = '';
    vi.useFakeTimers();
    vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(cb, 16));
  });

  it('a newer navigation cancels the previous retry loop', () => {
    // .layout-content is a SINGLETON — the shell mounts once and views swap
    // inside it. So a stale retry loop does not write to a detached node, it
    // writes to the page the user is now looking at, pinning it to the bottom
    // of its content. rAF throttling in a background tab widens the window.
    let scrollHeight = 800;                       // not yet scrollable
    document.body.innerHTML = '<div class="layout-content"></div>';
    const el = document.querySelector('.layout-content');
    let top = 0;
    Object.defineProperty(el, 'scrollHeight', { get: () => scrollHeight, configurable: true });
    Object.defineProperty(el, 'clientHeight', { get: () => 800, configurable: true });
    Object.defineProperty(el, 'scrollTop', {
      get: () => top,
      set: (v) => { top = Math.max(0, Math.min(v, scrollHeight - 800)); },
      configurable: true,
    });

    scrollHeight = 2000; el.scrollTop = 900; rememberScroll('/deep');
    scrollHeight = 800; el.scrollTop = 0;

    applyScroll('/deep', true);        // starts retrying for 900
    vi.advanceTimersByTime(16);        // can't reach it yet; reschedules
    applyScroll('/fresh', false);      // user navigates on — wants top
    vi.advanceTimersByTime(16);        // the FRESH loop does its single write

    // Only now does the old page's content finish arriving. Ordering matters:
    // if scrollHeight grows before the fresh write lands, the fresh write
    // happens last and the test passes even with the guard removed — which is
    // exactly how the first version of this test fooled itself.
    scrollHeight = 2000;
    for (let i = 0; i < 20; i += 1) vi.advanceTimersByTime(16);

    expect(el.scrollTop).toBe(0);      // the stale loop must not win
  });

  it('treats a URL change that keeps the same view as "not a screen change"', () => {
    // EstimateView router.replace()s /estimates/new → /estimates/:id after
    // autosaving a draft, WITHOUT unmounting (its own comment says so).
    // Scrolling to the top there yanks the user out of the form mid-edit.
    const EstimateView = { name: 'EstimateView' };
    const route = (path, comp) => ({ fullPath: path, matched: [{ components: { default: comp } }] });

    expect(sameViewComponent(route('/estimates/1', EstimateView), route('/estimates/new', EstimateView)))
      .toBe(true);
    expect(sameViewComponent(route('/jobs', { name: 'JobsView' }), route('/estimates/new', EstimateView)))
      .toBe(false);
  });

  it('does not treat two unmatched routes as the same view', () => {
    expect(sameViewComponent({ matched: [] }, { matched: [] })).toBe(false);
    expect(sameViewComponent(undefined, undefined)).toBe(false);
  });
});
