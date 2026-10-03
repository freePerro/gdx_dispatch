import { describe, expect, it } from "vitest";
import { ageInDays, bucketOutstanding, parseBreakpoints } from "./arAging";

const TODAY = new Date(2026, 9, 2, 15, 30); // 2026-10-02 afternoon, local

function inv(id, invoice_date, balance_due, extra = {}) {
  return { id, invoice_number: `INV-${id}`, status: "Sent", total: balance_due, balance_due, invoice_date, due_date: "", ...extra };
}

describe("parseBreakpoints", () => {
  it("parses, de-duplicates and sorts; drops junk, zero and negatives", () => {
    expect(parseBreakpoints("90, 30,60")).toEqual([30, 60, 90]);
    expect(parseBreakpoints("30 30 abc -5 0 45")).toEqual([30, 45]);
    expect(parseBreakpoints("1.5, 20")).toEqual([20]);
    expect(parseBreakpoints("")).toEqual([]);
  });
});

describe("ageInDays", () => {
  it("counts whole local days from the invoice date", () => {
    expect(ageInDays(inv(1, "2026-10-02", 1), "invoice", TODAY)).toBe(0);
    expect(ageInDays(inv(1, "2026-09-02", 1), "invoice", TODAY)).toBe(30);
  });
  it("takes the shop's day as a 'YYYY-MM-DD' string", () => {
    expect(ageInDays(inv(1, "2026-09-02", 1), "invoice", "2026-10-02")).toBe(30);
    expect(ageInDays(inv(1, "2026-09-02", 1, { due_date: "2026-10-03" }), "due", "2026-10-02")).toBe(-1);
  });
  it("is negative for a due date still ahead, null with no date", () => {
    expect(ageInDays(inv(1, "2026-10-01", 1, { due_date: "2026-10-12" }), "due", TODAY)).toBe(-10);
    expect(ageInDays(inv(1, "2026-10-01", 1), "due", TODAY)).toBeNull();
  });
});

describe("bucketOutstanding", () => {
  const book = [
    inv(1, "2026-10-02", 100),          // 0 days
    inv(2, "2026-09-02", 200),          // 30 days — upper edge of 0–30
    inv(3, "2026-09-01", 300),          // 31 days
    inv(4, "2026-07-01", 400),          // 93 days
    inv(5, "2026-09-20", 999, { status: "Paid" }),
    inv(6, "2026-09-20", 999, { status: "Draft" }),
    inv(7, "2026-09-20", 999, { status: "Void" }),
    inv(8, "2026-09-20", 0),
    inv(9, "2026-09-20", 50, { status: "Overdue" }), // 12 days
  ];

  it("puts every receivable in exactly one row and reconciles to the total", () => {
    const r = bucketOutstanding(book, { breakpoints: [30, 60, 90], today: TODAY });
    expect(r.rows.map((x) => x.label)).toEqual(["0–30 days", "31–60 days", "61–90 days", "91+ days"]);
    expect(r.rows.map((x) => x.total)).toEqual([350, 300, 0, 400]);
    expect(r.total).toBe(1050);
    expect(r.count).toBe(5);
    expect(r.rows.reduce((s, x) => s + x.total, 0)).toBe(r.total);
  });

  it("carries a running 0–N total for each age row", () => {
    const r = bucketOutstanding(book, { breakpoints: [30, 60, 90], today: TODAY });
    expect(r.rows.map((x) => [x.cumulativeLabel, x.cumulative])).toEqual([
      ["0–30 days", 350], ["0–60 days", 650], ["0–90 days", 650], ["All aged", 1050],
    ]);
  });

  it("re-cuts with custom ranges", () => {
    const r = bucketOutstanding(book, { breakpoints: [15], today: TODAY });
    expect(r.rows.map((x) => [x.label, x.total])).toEqual([["0–15 days", 150], ["16+ days", 900]]);
  });

  it("by due date: not-yet-due and no-due-date rows keep the total whole", () => {
    const due = [
      inv(1, "2026-09-01", 100, { due_date: "2026-10-15" }),
      inv(2, "2026-08-01", 200, { due_date: "2026-08-31" }), // 32 days past due
      inv(3, "2026-08-01", 300),                              // no due date
    ];
    const r = bucketOutstanding(due, { breakpoints: [30], basis: "due", today: TODAY });
    expect(r.rows.map((x) => [x.key, x.total])).toEqual([
      ["not_yet_due", 100], ["d1_30", 0], ["d31_plus", 200], ["no_date", 300],
    ]);
    expect(r.total).toBe(600);
  });

  it("by due date: due today is not yet due, so aged rows equal the Overdue card", () => {
    const due = [
      inv(1, "2026-10-02", 100, { due_date: "2026-10-02" }), // due today
      inv(2, "2026-09-01", 200, { due_date: "2026-10-01" }), // 1 day past due
    ];
    const r = bucketOutstanding(due, { breakpoints: [30], basis: "due", today: "2026-10-02" });
    expect(r.rows.map((x) => [x.key, x.label, x.total])).toEqual([
      ["not_yet_due", "Not yet due", 100], ["d1_30", "1–30 days", 200], ["d31_plus", "31+ days", 0],
    ]);
    expect(r.rows[1].cumulativeLabel).toBe("1–30 days");
    expect(r.rows.at(-1).cumulativeLabel).toBe("All overdue");
    expect(r.rows.at(-1).cumulative).toBe(200);
  });

  it("by invoice date: a future-dated invoice is labelled as such, not 'not yet due'", () => {
    const r = bucketOutstanding([inv(1, "2026-10-05", 50)], { breakpoints: [30], today: "2026-10-02" });
    expect(r.rows[0]).toMatchObject({ key: "not_yet_due", label: "Dated in the future", total: 50 });
  });

  it("falls back to the default ranges when none parse", () => {
    const r = bucketOutstanding(book, { breakpoints: [], today: TODAY });
    expect(r.rows).toHaveLength(4);
  });
});
