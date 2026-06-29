import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { shouldPoll, usePollingRefresh } from "../usePollingRefresh";

describe("shouldPoll (pure gate)", () => {
  it("runs only when visible and not paused", () => {
    expect(shouldPoll({ hidden: false, paused: false })).toBe(true);
  });
  it("skips when the tab is hidden", () => {
    expect(shouldPoll({ hidden: true, paused: false })).toBe(false);
  });
  it("skips when paused (drag / manual refresh in flight)", () => {
    expect(shouldPoll({ hidden: false, paused: true })).toBe(false);
  });
  it("skips when both hidden and paused", () => {
    expect(shouldPoll({ hidden: true, paused: true })).toBe(false);
  });
});

// Control document.hidden for tick/visibility tests.
function setHidden(value) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
}

describe("usePollingRefresh — tick gating", () => {
  beforeEach(() => setHidden(false));
  afterEach(() => {
    setHidden(false);
    vi.restoreAllMocks();
  });

  it("tick fires the callback when visible and not paused", () => {
    const cb = vi.fn();
    const { tick } = usePollingRefresh(cb, { isPaused: () => false });
    tick();
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("tick skips the callback when the tab is hidden", () => {
    setHidden(true);
    const cb = vi.fn();
    const { tick } = usePollingRefresh(cb, { isPaused: () => false });
    tick();
    expect(cb).not.toHaveBeenCalled();
  });

  it("tick skips the callback when isPaused() is true", () => {
    const cb = vi.fn();
    let paused = true;
    const { tick } = usePollingRefresh(cb, { isPaused: () => paused });
    tick();
    expect(cb).not.toHaveBeenCalled();
    paused = false;
    tick();
    expect(cb).toHaveBeenCalledTimes(1);
  });
});

describe("usePollingRefresh — visibility refresh on re-focus", () => {
  beforeEach(() => setHidden(false));
  afterEach(() => {
    setHidden(false);
    vi.restoreAllMocks();
  });

  it("refreshes immediately when the tab becomes visible", () => {
    const cb = vi.fn();
    const { onVisibility } = usePollingRefresh(cb, { isPaused: () => false });
    onVisibility();
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("does not refresh when the visibility event fires while still hidden", () => {
    setHidden(true);
    const cb = vi.fn();
    const { onVisibility } = usePollingRefresh(cb, { isPaused: () => false });
    onVisibility();
    expect(cb).not.toHaveBeenCalled();
  });

  it("does not refresh on focus while paused (mid-drag)", () => {
    const cb = vi.fn();
    const { onVisibility } = usePollingRefresh(cb, { isPaused: () => true });
    onVisibility();
    expect(cb).not.toHaveBeenCalled();
  });

  it("respects refreshOnVisible: false", () => {
    const cb = vi.fn();
    const { onVisibility } = usePollingRefresh(cb, { isPaused: () => false, refreshOnVisible: false });
    onVisibility();
    expect(cb).not.toHaveBeenCalled();
  });
});

describe("usePollingRefresh — interval lifecycle", () => {
  beforeEach(() => {
    setHidden(false);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    setHidden(false);
  });

  it("start() polls on the interval; stop() halts it", () => {
    const cb = vi.fn();
    const { start, stop } = usePollingRefresh(cb, { intervalMs: 1000, isPaused: () => false });
    start();
    vi.advanceTimersByTime(3000);
    expect(cb).toHaveBeenCalledTimes(3);
    stop();
    vi.advanceTimersByTime(5000);
    expect(cb).toHaveBeenCalledTimes(3); // no more ticks after stop
  });

  it("start() is idempotent (no double interval)", () => {
    const cb = vi.fn();
    const { start, stop } = usePollingRefresh(cb, { intervalMs: 1000, isPaused: () => false });
    start();
    start();
    vi.advanceTimersByTime(2000);
    expect(cb).toHaveBeenCalledTimes(2);
    stop();
  });
});
