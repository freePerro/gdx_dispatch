/**
 * --keyboard-inset: how much of the layout viewport the software keyboard covers.
 *
 * Exists because `interactive-widget=resizes-content` (index.html) is
 * Chromium/Gecko only. iOS Safari ignores it and shrinks only the VISUAL
 * viewport, so a fullscreen dialog keeps its full height and the keyboard
 * covers Save/Cancel. The same arithmetic gives ~0 on Chromium — where the
 * layout viewport already shrank — so nothing double-applies and there is no
 * user-agent branching.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { startKeyboardInsetTracking, stopKeyboardInsetTracking } from '../keyboardInset';

function fakeVisualViewport({ height, offsetTop = 0 } = {}) {
  const listeners = {};
  return {
    height, offsetTop, scale: 1,
    addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); },
    removeEventListener: (t, fn) => {
      listeners[t] = (listeners[t] || []).filter((f) => f !== fn);
    },
    emit: (t) => (listeners[t] || []).forEach((fn) => fn()),
    _listeners: listeners,
  };
}

const inset = () => document.documentElement.style.getPropertyValue('--keyboard-inset');

describe('keyboardInset', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(cb, 0));
    vi.stubGlobal('cancelAnimationFrame', (id) => clearTimeout(id));
    window.innerHeight = 844;
  });

  afterEach(() => {
    stopKeyboardInsetTracking();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('publishes the covered height when the keyboard opens', () => {
    const vv = fakeVisualViewport({ height: 844 });
    vi.stubGlobal('visualViewport', vv);
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });

    startKeyboardInsetTracking();
    vi.advanceTimersByTime(1);
    expect(inset()).toBe('0px');

    vv.height = 508;               // ~336px keyboard
    vv.emit('resize');
    vi.advanceTimersByTime(1);
    expect(inset()).toBe('336px');
    expect(document.documentElement.classList.contains('kb-open')).toBe(true);
  });

  it('counts the visual viewport offset, which iOS uses when focusing low fields', () => {
    const vv = fakeVisualViewport({ height: 500, offsetTop: 100 });
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
    startKeyboardInsetTracking();
    vi.advanceTimersByTime(1);
    // 844 - (500 + 100) = 244 — the offset is part of what is hidden.
    expect(inset()).toBe('244px');
  });

  it('returns to zero when the keyboard closes', () => {
    const vv = fakeVisualViewport({ height: 508 });
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
    startKeyboardInsetTracking();
    vi.advanceTimersByTime(1);
    expect(inset()).toBe('336px');

    vv.height = 844;
    vv.emit('resize');
    vi.advanceTimersByTime(1);
    expect(inset()).toBe('0px');
    expect(document.documentElement.classList.contains('kb-open')).toBe(false);
  });

  it('ignores small deltas — URL-bar drift is not a keyboard', () => {
    // iOS reports a few px as the address bar settles; treating that as a
    // keyboard would add stray padding to every dialog.
    const vv = fakeVisualViewport({ height: 800 });   // 44px delta
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
    startKeyboardInsetTracking();
    vi.advanceTimersByTime(1);
    expect(inset()).toBe('0px');
  });

  it('coalesces a burst of resize events into one write per frame', () => {
    const vv = fakeVisualViewport({ height: 844 });
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
    startKeyboardInsetTracking();
    vi.advanceTimersByTime(1);

    const spy = vi.spyOn(document.documentElement.style, 'setProperty');
    vv.height = 508;
    for (let i = 0; i < 10; i += 1) vv.emit('resize');   // keyboard slide-in
    vi.advanceTimersByTime(1);
    expect(spy).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });

  it('is a no-op without visualViewport, leaving the CSS fallback in charge', () => {
    Object.defineProperty(window, 'visualViewport', { value: undefined, configurable: true });
    expect(() => startKeyboardInsetTracking()).not.toThrow();
    expect(inset()).toBe('');            // unset → var(--keyboard-inset, 0px)
  });

  it('reports NO inset while the user is pinch-zoomed', () => {
    // The bug this guards: visualViewport.height is in CSS pixels, so at scale
    // 2 it is half of innerHeight — arithmetically identical to a very tall
    // keyboard. Without the scale check, pinching a dialog padded it by half
    // the screen. Zoom stays enabled on purpose (accessibility), so this case
    // is reachable by any user at any time.
    const vv = fakeVisualViewport({ height: 422 });   // 844/2, as if zoomed 2x
    vv.scale = 2;
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
    startKeyboardInsetTracking();
    vi.advanceTimersByTime(1);
    expect(inset()).toBe('0px');
    expect(document.documentElement.classList.contains('kb-open')).toBe(false);
  });

  it('still reports the keyboard at normal zoom', () => {
    const vv = fakeVisualViewport({ height: 508 });
    vv.scale = 1;
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
    startKeyboardInsetTracking();
    vi.advanceTimersByTime(1);
    expect(inset()).toBe('336px');
  });

  it('does not stack listeners if started twice', () => {
    const vv = fakeVisualViewport({ height: 844 });
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
    startKeyboardInsetTracking();
    startKeyboardInsetTracking();
    expect(vv._listeners.resize).toHaveLength(1);
  });
});
