/**
 * Line-category helpers for the customer pages. The grouping must match the
 * estimate PDF (core/pdf_generator.py _group_lines): first-appearance order,
 * never alphabetical, blank categories in one unheaded '' group.
 */
import { describe, expect, it } from "vitest";
import { groupLinesByCategory, lineCategoryMode, rowsGroupedByCategory } from "../lineCategories";

const LINES = [
  { description: "Spring", category: "Parts" },
  { description: "Door panel", category: "Door" },
  { description: "Haul away", category: null },
  { description: "Cable", category: " Parts " },
  { description: "Trip", category: "" },
];

describe("lineCategoryMode", () => {
  it("passes the two display modes and reads anything else as off", () => {
    expect(lineCategoryMode("column")).toBe("column");
    expect(lineCategoryMode("grouped")).toBe("grouped");
    expect(lineCategoryMode("off")).toBe("off");
    expect(lineCategoryMode(undefined)).toBe("off");
    expect(lineCategoryMode("bogus")).toBe("off");
  });
});

describe("groupLinesByCategory", () => {
  it("buckets in first-appearance order and keeps line order within a bucket", () => {
    const groups = groupLinesByCategory(LINES);
    expect(groups.map((g) => g.category)).toEqual(["Parts", "Door", ""]);
    expect(groups[0].lines.map((l) => l.description)).toEqual(["Spring", "Cable"]);
    expect(groups[2].lines.map((l) => l.description)).toEqual(["Haul away", "Trip"]);
  });

  it("handles no lines", () => {
    expect(groupLinesByCategory(undefined)).toEqual([]);
  });
});

describe("rowsGroupedByCategory", () => {
  it("flattens so each category's rows are adjacent, tagged with _category", () => {
    const rows = rowsGroupedByCategory(LINES);
    expect(rows.map((r) => r.description)).toEqual(["Spring", "Cable", "Door panel", "Haul away", "Trip"]);
    expect(rows.map((r) => r._category)).toEqual(["Parts", "Parts", "Door", "", ""]);
  });
});
