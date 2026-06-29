/**
 * Dispatch auto-refresh — poll veto across the optimistic-assign window.
 *
 * feat/daily-ux-improvements adds background board polling. An adversarial
 * audit caught a clobber: handleDrop clears draggingJobId BEFORE awaiting the
 * assign, and a same-date tech reassignment never refetches — so a poll
 * landing in the optimistic-write → PATCH window would overwrite the move with
 * stale server rows and visibly revert it.
 *
 * The fix wraps _doAssignJob in a pendingWrites counter that isPaused() also
 * checks. This spec pins that contract two ways:
 *  1. an isolated re-implementation proving the veto holds across the await;
 *  2. a static-source guard proving the real DispatchView wires it the same.
 */
import { describe, expect, it } from "vitest";
import { defineComponent, ref } from "vue";
import { mount, flushPromises } from "@vue/test-utils";

// A deferred promise so the test can inspect state mid-PATCH.
function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const Host = defineComponent({
  setup() {
    const refreshing = ref(false);
    const draggingJobId = ref(null);
    const pendingWrites = ref(0);
    const pollCount = ref(0);

    const isPaused = () =>
      refreshing.value || draggingJobId.value != null || pendingWrites.value > 0;

    // Mirrors usePollingRefresh's tick gate (visible tab assumed).
    function poll() {
      if (isPaused()) return;
      pollCount.value += 1;
    }

    // Mirrors _doAssignJob's guard wrapper.
    async function assign(patchPromise) {
      pendingWrites.value += 1;
      try {
        await patchPromise; // the in-flight PATCH
      } finally {
        pendingWrites.value -= 1;
      }
    }

    return { refreshing, draggingJobId, pendingWrites, isPaused, poll, pollCount, assign };
  },
  template: "<div>{{ pollCount }}</div>",
});

describe("dispatch poll veto across optimistic assign", () => {
  it("isPaused stays true for the whole PATCH window, then releases", async () => {
    const wrapper = mount(Host);
    const d = deferred();

    expect(wrapper.vm.isPaused()).toBe(false);
    const assigning = wrapper.vm.assign(d.promise);
    // PATCH in flight — drag flag already cleared, but pendingWrites holds.
    expect(wrapper.vm.pendingWrites).toBe(1);
    expect(wrapper.vm.isPaused()).toBe(true);

    // A poll firing during the window must be vetoed (no clobber).
    wrapper.vm.poll();
    expect(wrapper.vm.pollCount).toBe(0);

    d.resolve();
    await assigning;
    await flushPromises();

    expect(wrapper.vm.pendingWrites).toBe(0);
    expect(wrapper.vm.isPaused()).toBe(false);
    // Polls resume once the write settled.
    wrapper.vm.poll();
    expect(wrapper.vm.pollCount).toBe(1);
  });

  it("counter is balanced even if the PATCH rejects (no permanent veto leak)", async () => {
    const wrapper = mount(Host);
    const d = deferred();
    const assigning = wrapper.vm.assign(d.promise).catch(() => {});
    expect(wrapper.vm.isPaused()).toBe(true);
    d.resolve(Promise.reject(new Error("boom")));
    await assigning;
    await flushPromises();
    expect(wrapper.vm.pendingWrites).toBe(0);
    expect(wrapper.vm.isPaused()).toBe(false);
  });
});

describe("DispatchView.vue wiring (static-source guard)", () => {
  let SRC;
  it("loads source", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    SRC = readFileSync(join(__dirname, "..", "DispatchView.vue"), "utf8");
    expect(SRC.length).toBeGreaterThan(0);
  });

  it("withBoardWrite helper brackets the veto counter", () => {
    const start = SRC.indexOf("async function withBoardWrite(");
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 220);
    expect(span).toMatch(/pendingWrites\.value\s*\+=\s*1/);
    expect(span).toMatch(/pendingWrites\.value\s*-=\s*1/);
    expect(span).toMatch(/finally/);
  });

  // The first audit fix only covered _doAssignJob; a second audit found four
  // more optimistic-mutation paths. Pin that EVERY such path routes through
  // withBoardWrite so a future handler can't silently reopen the clobber hole.
  it.each([
    "_doAssignJob",
    "confirmDurationPrompt",
    "onTimelinePlace",
    "moveToHoldingArea",
    "releaseFromHoldingArea",
  ])("%s routes its optimistic mutation through withBoardWrite", (fn) => {
    const start = SRC.indexOf(`async function ${fn}(`);
    expect(start, `${fn} should exist`).toBeGreaterThan(-1);
    // Slice to the next top-level `async function ` so we only inspect this fn.
    const rest = SRC.slice(start + 10);
    const next = rest.indexOf("\nasync function ");
    const span = rest.slice(0, next === -1 ? 1200 : next);
    expect(span, `${fn} must call withBoardWrite`).toMatch(/withBoardWrite\(/);
  });

  it("isPaused predicate includes pendingWrites", () => {
    expect(SRC).toMatch(/isPaused:\s*\(\)\s*=>[^,\n]*pendingWrites\.value\s*>\s*0/);
  });

  it("pollBoard fetches with keepOnError so a transient blip never blanks the board", () => {
    const start = SRC.indexOf("async function pollBoard(");
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 600);
    expect(span).toMatch(/keepOnError:\s*true/);
    // fetchJobs must honor the flag (guarded blank, not unconditional).
    expect(SRC).toMatch(/if\s*\(!keepOnError\)\s*jobs\.value\s*=\s*\[\]/);
  });
});
