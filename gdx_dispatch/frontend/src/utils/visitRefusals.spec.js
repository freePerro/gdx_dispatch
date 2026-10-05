/**
 * visitRefusals — reading the flat 409 body useApi throws (multi-day jobs
 * plan §5.2a) and the wording built from it.
 */
import { describe, it, expect } from "vitest";
import {
  refusalOf, isNeedsAnswer, pointsAtAppointments, isDispatchManagerRole,
  formatShopDay, joinNames, rebookQuestion, notRevertedLines,
} from "./visitRefusals";

function apiError(status, body, message = body?.detail) {
  return Object.assign(new Error(message), { status, body });
}

describe("refusalOf", () => {
  it("reads code and fields off err.body, detail verbatim", () => {
    const r = refusalOf(apiError(409, { detail: "Mike is on site.", code: "crew_on_site", tech_ids: ["t1"] }));
    expect(r).toMatchObject({ code: "crew_on_site", detail: "Mike is on site.", tech_ids: ["t1"] });
  });
  it("is null for an error with no code (a plain failure)", () => {
    expect(refusalOf(apiError(500, { detail: "boom" }))).toBeNull();
    expect(refusalOf(new Error("network"))).toBeNull();
    expect(refusalOf(null)).toBeNull();
  });
  it("falls back to the message when detail is not a string", () => {
    const r = refusalOf(apiError(409, { detail: ["x"], code: "double_booked" }, "Double booked"));
    expect(r.detail).toBe("Double booked");
  });
});

describe("isNeedsAnswer / pointsAtAppointments", () => {
  const rebook = apiError(409, { detail: "x", code: "needs_answer", question: "rebook_closed_day" });
  const statusOnly = apiError(409, { detail: "x", code: "needs_answer", question: "status_only_arrival" });
  it("matches the question when one is named", () => {
    expect(isNeedsAnswer(rebook)).toBe(true);
    expect(isNeedsAnswer(rebook, "rebook_closed_day")).toBe(true);
    expect(isNeedsAnswer(rebook, "status_only_arrival")).toBe(false);
  });
  it("sends R1–R4, visit_arrived and status_only_arrival to the Appointments page", () => {
    for (const code of ["crew_on_site", "onto_booked_day", "two_open_visits", "double_booked", "visit_arrived"]) {
      expect(pointsAtAppointments({ code })).toBe(true);
    }
    expect(pointsAtAppointments(refusalOf(statusOnly))).toBe(true);
    expect(pointsAtAppointments(refusalOf(rebook))).toBe(false);
    expect(pointsAtAppointments({ code: "arrival_recorded" })).toBe(false);
    expect(pointsAtAppointments(null)).toBe(false);
  });
});

describe("isDispatchManagerRole", () => {
  it("is owner, admin, dispatcher or manager only", () => {
    expect(isDispatchManagerRole("manager")).toBe(true);
    expect(isDispatchManagerRole("owner")).toBe(true);
    expect(isDispatchManagerRole("admin")).toBe(true);
    expect(isDispatchManagerRole("dispatcher")).toBe(true);
    expect(isDispatchManagerRole("technician")).toBe(false);
    expect(isDispatchManagerRole(undefined)).toBe(false);
  });
});

describe("wording", () => {
  it("reads a shop day as a local calendar date", () => {
    expect(formatShopDay("2026-10-06")).toMatch(/^\w{3} 10\/6$/);
  });
  it("joins names", () => {
    expect(joinNames(["Mike"])).toBe("Mike");
    expect(joinNames(["Mike", "Sam"])).toBe("Mike and Sam");
    expect(joinNames(["A", "B", "C"])).toBe("A, B and C");
  });
  it("asks the rebook question with has / have", () => {
    expect(rebookQuestion(["Mike"], "2026-10-06")).toMatch(/^Mike already has a closed visit on \w{3} 10\/6\. Book them on that day again\?$/);
    expect(rebookQuestion(["Mike", "Sam"], "2026-10-06")).toContain("Mike and Sam already have");
    expect(rebookQuestion([], "2026-10-06")).toContain("A crew tech already has");
  });
  it("says one line per not_reverted item, naming why", () => {
    const lines = notRevertedLines([
      { field: "tap", reason: "no_record" },
      { field: "visit.start_at", reason: "double_book" },
      { field: "dispatch_status", reason: "crew_came" },
    ]);
    expect(lines).toHaveLength(3);
    expect(lines[0]).toMatch(/No tap was undone/);
    expect(lines[1]).toBe("The visit stays where it is — the tech already has another visit on that day.");
    expect(lines[2]).toBe("The job stays On site — the tech tapped again on that day.");
  });
});
