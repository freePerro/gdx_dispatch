/**
 * Day-view lane order — which sections the dispatcher can reach without
 * scrolling.
 *
 * This has now been wrong in BOTH directions inside one week, and each time it
 * shipped because nobody dragged a job on the real board:
 *
 *  - Originally the "New Jobs to Schedule" queue sat at order:2, below every
 *    tech column AND the holding areas. A freshly created job landed below the
 *    fold → "I made a job and it never showed up on dispatch".
 *  - The fix for that (#299) put the queue at order:1 and pushed the tech grid
 *    to order:3. That took the assign-to-tech drop targets below the fold
 *    instead, and you cannot scroll mid-drag — so assigning a job to a tech by
 *    dragging became impossible (Doug, 2026-08-10: "the dispatch board lost
 *    its spot to drop the job for the technician").
 *
 * Both lanes have to be reachable, so the two the dispatcher works BETWEEN are
 * adjacent — techs first, queue directly under, parking lots last. That
 * adjacency is the invariant; a bare `order:` number tells you nothing on its
 * own, so this asserts the relative sequence.
 *
 * 2026-10-05 (multi-day jobs plan D11): Partial Jobs is the one section
 * allowed between them. It is a drop SOURCE like the queue, it is hidden when
 * empty, and its cards are as short — so the queue is still one short reach
 * from the tech columns. Anything else wedged in there is the old regression.
 *
 * Static-source guard rather than a mount: DispatchView is too heavy for unit
 * tests (same reasoning as the holding-area and labor-exception specs).
 */
import { beforeAll, describe, expect, it } from "vitest";

let SRC;

beforeAll(async () => {
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  SRC = readFileSync(join(__dirname, "..", "DispatchView.vue"), "utf8");
  expect(SRC.length).toBeGreaterThan(0);
});

/** The `order:` value on the element whose opening tag matches `marker`. */
function orderOf(marker) {
  const at = SRC.indexOf(marker);
  expect(at, `marker not found in DispatchView.vue: ${marker}`).toBeGreaterThan(-1);
  // Scan back to the start of this element's opening tag, then find its order.
  const tagStart = SRC.lastIndexOf("<", at);
  const tag = SRC.slice(tagStart, at + marker.length + 200);
  const m = tag.match(/style="order:\s*(\d+)/);
  expect(m, `no inline order on the element carrying: ${marker}`).not.toBeNull();
  return Number(m[1]);
}

describe("dispatch day-view lane order", () => {
  it("puts the technician grid above the scheduling queue", () => {
    // The regression Doug hit: techs below the queue = drop targets off screen.
    expect(orderOf('class="tech-columns-grid"')).toBeLessThan(
      orderOf('data-testid="unassigned-section"'),
    );
  });

  it("keeps the scheduling queue above the holding areas", () => {
    // The ORIGINAL regression: queue buried under the parking lots.
    expect(orderOf('data-testid="unassigned-section"')).toBeLessThan(
      orderOf('class="holding-areas-section"'),
    );
  });

  it("leaves only Partial Jobs between the techs and the queue", () => {
    const tech = orderOf('class="tech-columns-grid"');
    const partial = orderOf('data-testid="partial-jobs"');
    const queue = orderOf('data-testid="unassigned-section"');
    expect([partial - tech, queue - partial]).toEqual([1, 1]);
    // Nothing else in the day view shares either slot.
    const orders = [...SRC.matchAll(/style="order:\s*(\d+)/g)].map((m) => Number(m[1]));
    expect(orders.filter((o) => o === partial)).toHaveLength(1);
  });

  it("hides Partial Jobs when it is empty, so the usual board is unchanged", () => {
    const at = SRC.indexOf('data-testid="partial-jobs"');
    const tagStart = SRC.lastIndexOf("<Card", at);
    expect(SRC.slice(tagStart, at)).toMatch(/v-if="visiblePartialJobs\.length"/);
  });

  it("keeps the empty-state holding-areas button with its own section", () => {
    // #299 moved the holding-areas section but left its v-else behind at
    // order:1, which floats a "Set Up Holding Areas" button up next to the
    // tech grid on any board with no holding areas configured. The order lives
    // on the wrapping v-else <div>, not on the Button itself.
    const at = SRC.indexOf('data-testid="seed-holding-areas"');
    expect(at).toBeGreaterThan(-1);
    const wrapper = SRC.lastIndexOf("<div v-else", at);
    expect(wrapper, "seed-holding-areas is no longer in a v-else div").toBeGreaterThan(-1);
    const m = SRC.slice(wrapper, at).match(/style="order:\s*(\d+)/);
    expect(m, "the holding-areas v-else lost its inline order").not.toBeNull();
    expect(Number(m[1])).toBe(orderOf('class="holding-areas-section"'));
  });

  it("still lets the queue accept a drop (it is a drop target, not just a list)", () => {
    const at = SRC.indexOf('data-testid="unassigned-section"');
    expect(SRC.slice(at, at + 400)).toMatch(/@drop\.prevent="moveToScheduleQueue/);
  });

  it("delegates the assign-to-tech drop to TechTimelineColumn", () => {
    // The tech Card wrapper deliberately has no @drop — the timeline body owns
    // it. If that delegation ever breaks, dragging onto a tech silently no-ops.
    expect(SRC).toMatch(/<TechTimelineColumn[\s\S]{0,400}@place="onTimelinePlace"/);
  });
});
