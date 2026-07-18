import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import DoorSpecList from "../DoorSpecList.vue";

// Two doors like the Swenstad job — same model, different size. Each identity
// carries Price (CHI's cost) which the install view must NOT render.
const doors = [
  {
    line_id: "a", quantity: 1,
    identity: { Number: "QCD100", Size: "14'0\" x 12'0\"", Model: "Skyline 2127", Color: "Sandstone", Price: "2890.00" },
    installer: { Spring: "Torsion", "Spring Count": "1 PR", "Wire Size": "0.273", Track: "3 IN.", "Date Created": "07/16/2026" },
    windows: [],
  },
  {
    line_id: "b", quantity: 1,
    identity: { Number: "QCD200", Size: "10'0\" x 10'0\"", Model: "Skyline 2127", Color: "Sandstone", Price: "1980.00" },
    installer: { Spring: "Torsion" }, windows: [],
  },
];

describe("DoorSpecList", () => {
  it("lists every door by size with its QCD, collapsed, and never shows price", () => {
    const w = mount(DoorSpecList, { props: { doors } });
    expect(w.findAll('[data-testid^="door-toggle-"]')).toHaveLength(2);
    expect(w.text()).toContain("14'0\" x 12'0\"");
    expect(w.text()).toContain("10'0\" x 10'0\"");
    // QCD number is shown in each header (cross-references the CHI order)
    expect(w.text()).toContain("QCD100");
    expect(w.text()).toContain("QCD200");
    // multiple doors start collapsed
    expect(w.find('[data-testid="door-body-0"]').exists()).toBe(false);
    // price is hidden even when collapsed AND (below) when expanded
    expect(w.text()).not.toContain("2890");
    expect(w.text()).not.toContain("1980");
  });

  it("distinguishes two doors of the SAME size by their QCD number", () => {
    const pair = [
      { line_id: "p1", quantity: 1, identity: { Number: "QCD777", Size: "9'0\" x 7'0\"", Model: "2283", Color: "White" }, installer: { Spring: "Torsion" }, windows: [] },
      { line_id: "p2", quantity: 1, identity: { Number: "QCD888", Size: "9'0\" x 7'0\"", Model: "2283", Color: "White" }, installer: { Spring: "Torsion" }, windows: [] },
    ];
    const w = mount(DoorSpecList, { props: { doors: pair } });
    // Two distinct rows, both 9x7, told apart by QCD.
    expect(w.findAll('[data-testid^="door-toggle-"]')).toHaveLength(2);
    const h0 = w.find('[data-testid="door-toggle-0"]').text();
    const h1 = w.find('[data-testid="door-toggle-1"]').text();
    expect(h0).toContain("9'0\" x 7'0\"");
    expect(h1).toContain("9'0\" x 7'0\"");
    expect(h0).toContain("QCD777");
    expect(h1).toContain("QCD888");
  });

  it("expands a door on click to reveal its build spec, still no price", async () => {
    const w = mount(DoorSpecList, { props: { doors } });
    await w.find('[data-testid="door-toggle-0"]').trigger("click");
    const body = w.find('[data-testid="door-body-0"]');
    expect(body.exists()).toBe(true);
    // The full spring detail surfaces — not just "Torsion".
    expect(body.text()).toContain("Spring Count");
    expect(body.text()).toContain("Wire Size");
    expect(w.text()).not.toContain("2890"); // price stays hidden
    expect(body.text()).not.toContain("Date Created"); // capture metadata hidden
    // Model/Color are in the header, not repeated in the grid rows
    expect(body.text()).not.toContain("Skyline 2127");
  });

  it("opens a lone door by default (nothing to choose between)", () => {
    const w = mount(DoorSpecList, { props: { doors: [doors[0]] } });
    expect(w.find('[data-testid="door-body-0"]').exists()).toBe(true);
  });

  it("falls back to a generic size label, not the order number, when Size is absent", () => {
    const w = mount(DoorSpecList, {
      props: { doors: [{ line_id: "x", quantity: 1, identity: { Number: "QCD999", Model: "M" }, installer: {}, windows: [] }] },
    });
    // The size HEADER must not be the order number (the body may still list it).
    expect(w.find('[data-testid="door-toggle-0"] .door-size').text()).toBe("Door");
  });
});
