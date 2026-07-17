/**
 * Labor exceptions card — the office's self-clearing timeclock review.
 *
 * Doug 2026-07-17: a tech is paid start-of-day to end-of-day, and "it should
 * be the dispatcher or office personel that get told about the discrepency."
 * He also said reports "get forgotten and annoying" — which is exactly what
 * happened to `core/recommendations.py`: it has no frontend renderer at all,
 * so everything filed there is invisible on arrival.
 *
 * So this is not a report. It renders on the board dispatch already has open,
 * only when something is wrong, and correcting the shift IS the dismissal.
 * The anti-nag property is the whole design, so it is what gets pinned:
 *
 *  1. an isolated re-implementation of the card's gate (DispatchView is too
 *     heavy for unit tests — same reasoning as the holding-area specs);
 *  2. a static-source guard proving the real DispatchView wires it the same.
 */
import { describe, expect, it } from "vitest";
import { defineComponent, ref } from "vue";
import { mount, flushPromises } from "@vue/test-utils";

const Host = defineComponent({
  props: { rows: { type: Array, default: () => [] }, fails: Boolean },
  setup(props) {
    const laborExceptions = ref([]);
    async function loadLaborExceptions() {
      try {
        if (props.fails) throw new Error("403");
        laborExceptions.value = Array.isArray(props.rows) ? props.rows : [];
      } catch {
        laborExceptions.value = [];
      }
    }
    return { laborExceptions, loadLaborExceptions };
  },
  template: `
    <div>
      <div v-if="laborExceptions.length" data-testid="labor-exceptions">
        <span data-testid="count">{{ laborExceptions.length }}</span>
        <div v-for="r in laborExceptions" :key="r.entry_id" class="row">
          {{ r.tech_name || 'Unknown tech' }} — {{ r.detail }}
        </div>
      </div>
    </div>
  `,
});

async function mountWith(rows, fails = false) {
  const w = mount(Host, { props: { rows, fails } });
  await w.vm.loadLaborExceptions();
  await flushPromises();
  return w;
}

const OPEN_SHIFT = {
  entry_id: "e1",
  kind: "open_shift",
  tech_name: "Michael",
  detail: "Still clocked in — never clocked out.",
  hours: 215,
};

describe("labor exceptions card", () => {
  it("does not render at all on a clean shop", async () => {
    const w = await mountWith([]);
    // Not "renders empty" — does not exist. A card that is always present is
    // a card the office learns to scroll past.
    expect(w.find('[data-testid="labor-exceptions"]').exists()).toBe(false);
  });

  it("renders when a shift needs correcting", async () => {
    const w = await mountWith([OPEN_SHIFT]);
    expect(w.find('[data-testid="labor-exceptions"]').exists()).toBe(true);
    expect(w.find('[data-testid="count"]').text()).toBe("1");
    expect(w.text()).toContain("Michael");
    expect(w.text()).toContain("never clocked out");
  });

  it("labels an unresolvable tech rather than dropping the row", async () => {
    // 10 of 39 prod shift rows have a technician_id matching no user. An
    // orphaned shift is still a real discrepancy.
    const w = await mountWith([{ ...OPEN_SHIFT, tech_name: null }]);
    expect(w.text()).toContain("Unknown tech");
  });

  it("stays invisible when the caller is not allowed to read it", async () => {
    // Techs get a 403 by design; the board must not break or show an error.
    const w = await mountWith([OPEN_SHIFT], true);
    expect(w.find('[data-testid="labor-exceptions"]').exists()).toBe(false);
  });

  it("vanishes once the shifts are corrected", async () => {
    // The fix IS the dismissal — no "mark as read" to forget.
    const w = await mountWith([OPEN_SHIFT]);
    expect(w.find('[data-testid="labor-exceptions"]').exists()).toBe(true);
    await w.setProps({ rows: [] });
    await w.vm.loadLaborExceptions();
    await flushPromises();
    expect(w.find('[data-testid="labor-exceptions"]').exists()).toBe(false);
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

  it("gates the card on there being rows", () => {
    expect(SRC).toMatch(/v-if="laborExceptions\.length"[\s\S]{0,80}data-testid="labor-exceptions"/);
  });

  it("loads from the office-gated endpoint on mount", () => {
    expect(SRC).toContain("/api/timeclock/exceptions");
    const start = SRC.indexOf("onMounted(async () => {");
    expect(start).toBeGreaterThan(-1);
    expect(SRC.slice(start, start + 260)).toMatch(/loadLaborExceptions\(\)/);
  });

  it("swallows the tech 403 instead of breaking the board", () => {
    const start = SRC.indexOf("async function loadLaborExceptions(");
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 420);
    expect(span).toMatch(/catch/);
    expect(span).toMatch(/laborExceptions\.value = \[\]/);
  });
});
