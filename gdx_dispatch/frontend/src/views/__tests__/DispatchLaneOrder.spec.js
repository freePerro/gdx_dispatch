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
 * 2026-10-16 (Doug: the holding lanes were "too far apart to grab and put on
 * another lane"): the parking lots left the bottom of this column. They are
 * a compact strip ABOVE the board — every lane two rows of small cards with
 * its own scroll — so a lane, another lane and any tech are on one screen.
 * The techs-then-queue adjacency above is unchanged.
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

  it("draws the holding areas as one strip above the day board, not under it", () => {
    // The ORIGINAL regression was the queue buried under the parking lots;
    // the 2026-10-16 one was the lanes buried under the tech columns.
    const strip = SRC.indexOf('data-testid="lane-strip"');
    const dayView = SRC.indexOf("<!-- Day View -->");
    expect(strip).toBeGreaterThan(-1);
    expect(strip).toBeLessThan(dayView);
    expect(SRC.slice(strip - 120, strip)).toMatch(/v-if="viewMode === 'day' \|\| viewMode === 'week'"/);
    // Nothing in the day column is drawn as a holding-area lane any more.
    expect(SRC.indexOf('moveToHoldingArea($event', dayView)).toBe(-1);
  });

  it("keeps every lane to two rows that scroll on their own", () => {
    const rule = SRC.match(/^\.week-queue-chips \{([^}]*)\}/m);
    expect(rule, "the lane chip list lost its rule").not.toBeNull();
    expect(rule[1]).toMatch(/max-height:\s*3\.4rem/);
    expect(rule[1]).toMatch(/overflow-y:\s*auto/);
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

  it("keeps the empty-state holding-areas button with its own strip", () => {
    // #299 moved the holding-areas section but left its empty state behind,
    // which floated a "Set Up Holding Areas" button somewhere else. It is
    // the strip's own v-else-if now.
    const at = SRC.indexOf('data-testid="seed-holding-areas"');
    expect(at).toBeGreaterThan(-1);
    const strip = SRC.indexOf('data-testid="lane-strip"');
    expect(at).toBeGreaterThan(strip);
    expect(at).toBeLessThan(SRC.indexOf("<!-- Day View -->"));
    expect(SRC.slice(SRC.lastIndexOf("<div v-else-if", at), at)).toMatch(/viewMode === 'day'/);
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
