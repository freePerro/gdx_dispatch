import { describe, it, expect } from "vitest";
import { readPrefs, writePrefs } from "../listPrefs";

const SCHEMA = {
  activeStatus: { default: "All", valid: (v) => ["All", "Scheduled", "Complete"].includes(v) },
  searchQuery: { default: "", valid: (v) => typeof v === "string" },
};

describe("readPrefs", () => {
  it("returns all defaults when raw is null (never written)", () => {
    expect(readPrefs(null, SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "" });
  });

  it("returns all defaults when raw is undefined (defensive)", () => {
    expect(readPrefs(undefined, SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "" });
  });

  it("reads back stored values verbatim when valid", () => {
    const raw = JSON.stringify({ activeStatus: "Scheduled", searchQuery: "smith" });
    expect(readPrefs(raw, SCHEMA)).toEqual({ activeStatus: "Scheduled", searchQuery: "smith" });
  });

  it("discards a stored value that fails its validator and uses the default", () => {
    // 'Archived' is not in the valid set (e.g. a tab removed in a later
    // release). Must fall back to 'All' so the list never silently empties.
    const raw = JSON.stringify({ activeStatus: "Archived", searchQuery: "x" });
    expect(readPrefs(raw, SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "x" });
  });

  it("fills missing keys from defaults (forward-compat with added prefs)", () => {
    const raw = JSON.stringify({ activeStatus: "Complete" });
    expect(readPrefs(raw, SCHEMA)).toEqual({ activeStatus: "Complete", searchQuery: "" });
  });

  it("falls back to defaults on malformed JSON", () => {
    expect(readPrefs("{not json", SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "" });
  });

  it("falls back to defaults when JSON is an array (not a plain object)", () => {
    expect(readPrefs("[1,2,3]", SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "" });
  });

  it("falls back to defaults when JSON is a primitive", () => {
    expect(readPrefs('"just a string"', SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "" });
    expect(readPrefs("42", SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "" });
  });

  it("validates a wrong-typed value (number where string expected)", () => {
    const raw = JSON.stringify({ activeStatus: "All", searchQuery: 99 });
    expect(readPrefs(raw, SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "" });
  });
});

describe("writePrefs", () => {
  it("serializes only schema keys, dropping unrelated state", () => {
    const out = writePrefs(
      { activeStatus: "Scheduled", searchQuery: "x", secretRef: "leak" },
      SCHEMA,
    );
    expect(JSON.parse(out)).toEqual({ activeStatus: "Scheduled", searchQuery: "x" });
  });

  it("round-trips through readPrefs", () => {
    const values = { activeStatus: "Complete", searchQuery: "door" };
    expect(readPrefs(writePrefs(values, SCHEMA), SCHEMA)).toEqual(values);
  });

  it("serializes undefined refs as their JSON form (read back as default via validator)", () => {
    // A missing ref value -> undefined -> JSON drops the key -> readPrefs
    // fills the default. Proves the write/read pair is self-healing.
    const out = writePrefs({ activeStatus: undefined, searchQuery: "y" }, SCHEMA);
    expect(readPrefs(out, SCHEMA)).toEqual({ activeStatus: "All", searchQuery: "y" });
  });
});
