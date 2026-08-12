/**
 * Publish the software keyboard's height as `--keyboard-inset` on <html>.
 *
 * Why this exists even though index.html sets `interactive-widget=resizes-content`:
 * that keyword is Chromium/Gecko only. iOS Safari ignores it and resizes only
 * the VISUAL viewport, so the layout viewport — and therefore every viewport
 * unit, including the `100svh` a fullscreen dialog is sized with — keeps its
 * full height and the keyboard covers the bottom of the dialog. That is where
 * Save and Cancel live.
 *
 * The measurement is the same on both platforms:
 *     layout height - (visual viewport height + its offsetTop)
 * On Chromium with resizes-content the layout viewport shrinks with the
 * keyboard, so this evaluates to ~0 and nothing double-applies. On iOS it
 * evaluates to the keyboard height. One mechanism, no branching on user agent.
 *
 * CSS consumes it (see responsive.css). Kept out of components on purpose:
 * a single listener for the whole app rather than one per dialog.
 */

// Below this we treat the delta as chrome/rounding noise rather than a keyboard.
// iOS reports a few px of drift as the URL bar settles.
const NOISE_PX = 60;

let started = false;
let raf = 0;

function apply() {
  raf = 0;
  const vv = window.visualViewport;
  if (!vv) return;
  // PINCH-ZOOM PRODUCES THE SAME SIGNATURE AS A KEYBOARD, and this is not a
  // hypothetical: visualViewport.height is reported in CSS pixels, so at scale
  // 2 it is half of innerHeight — indistinguishable from a very tall keyboard
  // by the arithmetic below. The viewport meta deliberately leaves zoom enabled
  // (never disable it, it is an accessibility control), so a user pinching a
  // dialog would otherwise get ~half the screen of phantom padding.
  // Above scale 1 the layout/visual gap is zoom, not a keyboard; report none.
  const zoomed = typeof vv.scale === 'number' && vv.scale > 1.01;
  // offsetTop matters: iOS scrolls the visual viewport within the layout
  // viewport when focusing a field near the bottom, and that shift is part of
  // the area the keyboard is covering.
  const covered = window.innerHeight - (vv.height + vv.offsetTop);
  const inset = !zoomed && covered > NOISE_PX ? Math.round(covered) : 0;
  document.documentElement.style.setProperty('--keyboard-inset', `${inset}px`);
  // A boolean hook for anything that needs to hide chrome while typing.
  document.documentElement.classList.toggle('kb-open', inset > 0);
}

function schedule() {
  // resize/scroll on the visual viewport fire at animation frequency while the
  // keyboard slides in; coalesce so we write the custom property once a frame.
  if (!raf) raf = requestAnimationFrame(apply);
}

export function startKeyboardInsetTracking() {
  if (started || typeof window === 'undefined') return;
  const vv = window.visualViewport;
  // No visualViewport (old browsers, jsdom) → leave the property unset. The CSS
  // fallback is `var(--keyboard-inset, 0px)`, so absence is safe.
  if (!vv) return;
  started = true;
  vv.addEventListener('resize', schedule);
  vv.addEventListener('scroll', schedule);
  apply();
}

// Exported for tests; the app never needs to stop tracking.
export function stopKeyboardInsetTracking() {
  const vv = window.visualViewport;
  if (!started || !vv) return;
  vv.removeEventListener('resize', schedule);
  vv.removeEventListener('scroll', schedule);
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  started = false;
  document.documentElement.style.removeProperty('--keyboard-inset');
  document.documentElement.classList.remove('kb-open');
}
