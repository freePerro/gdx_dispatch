/**
 * A booking longer than the tech's day (Doug, 2026-10-18): the arithmetic
 * behind DayOverflowDialog. Hours are wall clock; workdays use the
 * effective_workdays bitmask (Monday 1 … Saturday 32, Sunday 64).
 */
import { describe, expect, it } from "vitest";
import {
  hoursForCrew, hoursLeftInDay, nextWorkdays, shiftHours, splitOverDays,
} from "../useScheduleDrop";

const MON_FRI = 31;

describe("shiftHours / hoursLeftInDay", () => {
  it("measures from the drop time to shift end", () => {
    const shift = shiftHours("08:00", "17:00");
    expect(shift).toEqual({ start: 8, end: 17 });
    expect(hoursLeftInDay(14, shift)).toBe(3);
    expect(hoursLeftInDay(8.5, shift)).toBe(8.5);
  });
  it("gives the whole shift to a drop with no set time or before the shift", () => {
    const shift = shiftHours("07:30", "16:00");
    expect(hoursLeftInDay(null, shift)).toBe(8.5);
    expect(hoursLeftInDay(6, shift)).toBe(8.5);
  });
  it("is 0 after shift end and null with no shift to measure", () => {
    expect(hoursLeftInDay(18, shiftHours("08:00", "17:00"))).toBe(0);
    expect(shiftHours(null, "17:00")).toBeNull();
    expect(shiftHours("17:00", "08:00")).toBeNull();
    expect(hoursLeftInDay(9, null)).toBeNull();
  });
});

describe("hoursForCrew", () => {
  it("shares the same man-hours, rounded up to the quarter hour", () => {
    expect(hoursForCrew(14, 1, 2)).toBe(7);
    expect(hoursForCrew(7, 2, 1)).toBe(14);
    expect(hoursForCrew(10, 1, 3)).toBe(3.5);
  });
});

describe("nextWorkdays", () => {
  it("skips the days the tech does not work", () => {
    // 2026-10-23 is a Friday.
    expect(nextWorkdays("2026-10-23", MON_FRI, 2)).toEqual(["2026-10-26", "2026-10-27"]);
    expect(nextWorkdays("2026-10-23", MON_FRI | 32, 1)).toEqual(["2026-10-24"]);
  });
  it("returns what it found when the mask has no workdays", () => {
    expect(nextWorkdays("2026-10-23", 0, 1)).toEqual([]);
  });
});

describe("splitOverDays", () => {
  it("fills the first day to shift end, then whole shifts, the last holding the rest", () => {
    expect(splitOverDays({ day: "2026-10-21", hours: 21, firstHours: 9, shiftLength: 9, mask: MON_FRI }))
      .toEqual([
        { day: "2026-10-21", hours: 9 },
        { day: "2026-10-22", hours: 9 },
        { day: "2026-10-23", hours: 3 },
      ]);
  });
  it("carries over a weekend", () => {
    expect(splitOverDays({ day: "2026-10-23", hours: 5, firstHours: 3, shiftLength: 9, mask: MON_FRI }))
      .toEqual([{ day: "2026-10-23", hours: 3 }, { day: "2026-10-26", hours: 2 }]);
  });
  it("cannot split with nothing left today, no shift, no workdays, or past the 20-day cap", () => {
    expect(splitOverDays({ day: "2026-10-21", hours: 5, firstHours: 0, shiftLength: 9, mask: MON_FRI })).toBeNull();
    expect(splitOverDays({ day: "2026-10-21", hours: 5, firstHours: 2, shiftLength: 0, mask: MON_FRI })).toBeNull();
    expect(splitOverDays({ day: "2026-10-21", hours: 5, firstHours: 2, shiftLength: 9, mask: 0 })).toBeNull();
    expect(splitOverDays({ day: "2026-10-21", hours: 200, firstHours: 9, shiftLength: 9, mask: MON_FRI })).toBeNull();
  });
});
