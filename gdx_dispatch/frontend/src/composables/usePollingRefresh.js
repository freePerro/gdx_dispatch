import { onMounted, onBeforeUnmount } from "vue";

// Decide whether a poll tick should actually run. Pure + exported so the
// gating logic is unit-testable without fake timers.
//   hidden  document.hidden — never poll a backgrounded tab (saves the
//           server from N idle dispatcher tabs hammering it overnight)
//   paused  caller-supplied veto — e.g. a drag is in progress or a manual
//           refresh is already in flight; polling then would yank the board
//           out from under the user / double-fetch.
export function shouldPoll({ hidden, paused }) {
  return !hidden && !paused;
}

// Background auto-refresh for a view. Runs `callback` on an interval, but
// skips ticks while the tab is hidden or while `isPaused()` is true, and
// (by default) fires an immediate refresh when the tab regains focus so a
// returning user sees fresh data without waiting a full interval.
//
//   callback         async fn doing the refetch (must guard its own overlap)
//   options.intervalMs       poll period (default 45s)
//   options.isPaused         () => boolean veto checked every tick + on focus
//   options.refreshOnVisible refresh on tab re-focus (default true)
//
// Registers its own onMounted/onBeforeUnmount, so call it from setup().
export function usePollingRefresh(callback, options = {}) {
  const intervalMs = options.intervalMs ?? 45_000;
  const isPaused = options.isPaused ?? (() => false);
  const refreshOnVisible = options.refreshOnVisible ?? true;
  let timer = null;

  function tick() {
    const hidden = typeof document !== "undefined" && document.hidden;
    if (!shouldPoll({ hidden, paused: isPaused() })) return;
    callback();
  }

  function onVisibility() {
    if (typeof document !== "undefined" && document.hidden) return;
    if (!refreshOnVisible) return;
    if (isPaused()) return;
    callback();
  }

  function start() {
    if (timer) return;
    timer = setInterval(tick, intervalMs);
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibility);
    }
  }

  function stop() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibility);
    }
  }

  onMounted(start);
  onBeforeUnmount(stop);

  // tick/onVisibility returned for unit tests + manual control.
  return { start, stop, tick, onVisibility };
}
