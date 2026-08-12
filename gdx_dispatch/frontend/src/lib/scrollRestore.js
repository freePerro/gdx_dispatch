/**
 * Scroll restoration for the app shell.
 *
 * The obvious implementation — Vue Router's `scrollBehavior` returning
 * `savedPosition` — is a NO-OP in this app. `body` is `overflow: hidden`
 * (base.css) and the thing that actually scrolls is `.layout-content`
 * (`overflow: auto`, AppLayout.vue). The router scrolls the WINDOW, which never
 * moves inside the shell. So positions are recorded and restored on that
 * element by hand.
 *
 * Behaviour, which is what a phone user expects:
 *   - back/forward → the list comes back where you left it
 *   - any other navigation → start at the top
 *
 * Routes rendered outside the shell (login, the customer portal) have no
 * `.layout-content`; those fall back to normal window scrolling.
 */

const SCROLLER = '.layout-content';

// fullPath → scrollTop. A Map, not sessionStorage: this is per-session UI
// state, and persisting it would restore a stale offset into a list whose
// contents have since changed.
const positions = new Map();
// Bound so a long session on a big app doesn't grow without limit.
const MAX_ENTRIES = 50;

function scroller() {
  return document.querySelector(SCROLLER);
}

export function rememberScroll(fullPath) {
  const el = scroller();
  if (!el || !fullPath) return;
  if (positions.size >= MAX_ENTRIES) {
    // Drop the oldest insertion — Map preserves insertion order.
    positions.delete(positions.keys().next().value);
  }
  positions.set(fullPath, el.scrollTop);
}

export function forgetScroll(fullPath) {
  positions.delete(fullPath);
}

/** Exposed for tests. */
export function savedScrollFor(fullPath) {
  return positions.get(fullPath);
}

export function clearScrollPositions() {
  positions.clear();
}

/**
 * Apply the scroll position for `fullPath`.
 *
 * `isPop` comes from the router's `savedPosition` argument, which vue-router
 * only supplies on a popstate (back/forward) navigation — that is the signal,
 * even though its value is a window position we cannot use.
 *
 * Views are lazily imported and their data arrives after mount, so the target
 * is not scrollable on the first frame. Retry across a few frames until the
 * content is tall enough, then stop. Without this the restore silently clamps
 * to 0 and looks like it never worked.
 */
let generation = 0;

export function applyScroll(fullPath, isPop, { frames = 12 } = {}) {
  const target = isPop ? (positions.get(fullPath) ?? 0) : 0;
  let tries = 0;
  // Invalidate any retry loop still running from a previous navigation.
  // `.layout-content` is a SINGLETON — the shell mounts once and the view
  // swaps inside it — so a stale loop does not harmlessly write to a detached
  // node, it writes to the page the user is looking at NOW. Navigate twice
  // inside the retry window (trivial: rAF is throttled in a background tab)
  // and the old loop pins the new page to the bottom of its content.
  const mine = ++generation;

  const step = () => {
    if (mine !== generation) return;       // superseded by a newer navigation
    const el = scroller();
    if (!el) return;                       // outside the shell — nothing to do
    if (target === 0) { el.scrollTop = 0; return; }
    const reachable = el.scrollHeight - el.clientHeight;
    if (reachable >= target) {
      el.scrollTop = target;
      return;                              // content is tall enough: done
    }
    el.scrollTop = reachable;              // best effort while content grows
    if (++tries < frames) requestAnimationFrame(step);
  };

  requestAnimationFrame(step);
}

/**
 * Did this navigation actually swap the view?
 *
 * Scrolling to the top is right when you move to a different screen and wrong
 * when the URL changes underneath a screen that stays mounted. The app does the
 * latter deliberately: EstimateView POSTs a draft and `router.replace`s
 * /estimates/new → /estimates/:id *without unmounting* (its own comment says
 * so), and several views strip query params with `router.replace`. Resetting
 * there yanks the user to the top of the form they are filling in.
 */
export function sameViewComponent(to, from) {
  const leaf = (r) => r?.matched?.[r.matched.length - 1]?.components?.default;
  const a = leaf(to);
  return Boolean(a) && a === leaf(from);
}

/**
 * Wire the router up. Returns the router for chaining.
 */
export function installScrollRestore(router) {
  router.beforeEach((to, from, next) => {
    if (from?.fullPath) rememberScroll(from.fullPath);
    next();
  });
  return router;
}
