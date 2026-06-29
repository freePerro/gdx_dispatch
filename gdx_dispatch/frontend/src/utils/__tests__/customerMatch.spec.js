import { describe, it, expect } from "vitest";
import {
  normalizePhone,
  normalizeName,
  normalizeEmail,
  findDuplicateMatch,
  bestLookupTerm,
  lookupTerms,
} from "../customerMatch";

describe("normalizers", () => {
  it("normalizePhone strips all non-digits", () => {
    expect(normalizePhone("(555) 123-4567")).toBe("5551234567");
    expect(normalizePhone(null)).toBe("");
  });
  it("normalizePhone drops a leading US country code (11-digit '1…')", () => {
    // "+1 555…" and local "555…" must compare equal so a +1 query still
    // matches a locally-stored number.
    expect(normalizePhone("+1 555.123.4567")).toBe("5551234567");
    expect(normalizePhone("15551234567")).toBe("5551234567");
    // Not an 11-digit "1…" — left alone.
    expect(normalizePhone("25551234567")).toBe("25551234567");
    expect(normalizePhone("5551234567")).toBe("5551234567");
  });

  it("findDuplicateMatch matches +1-typed input against a locally-stored phone", () => {
    const m = findDuplicateMatch({ phone: "+1 (555) 123-4567" }, LIST);
    expect(m).toEqual({ customer: LIST[0], on: "phone" });
  });
  it("normalizeName trims, lowercases, collapses whitespace", () => {
    expect(normalizeName("  John   Smith ")).toBe("john smith");
    expect(normalizeName("JOHN SMITH")).toBe("john smith");
  });
  it("normalizeEmail trims + lowercases", () => {
    expect(normalizeEmail("  John@Example.COM ")).toBe("john@example.com");
  });
});

const LIST = [
  { id: 1, name: "John Smith", phone: "(555) 123-4567", email: "john@example.com" },
  { id: 2, name: "Jane Doe", phone: "555-999-0000", email: "jane@example.com" },
  { id: 3, name: "Bob Jones", phone: "", email: "" },
];

describe("findDuplicateMatch", () => {
  it("matches on phone regardless of formatting", () => {
    const m = findDuplicateMatch({ name: "Johnny", phone: "5551234567" }, LIST);
    expect(m).toEqual({ customer: LIST[0], on: "phone" });
  });

  it("matches on email when phone differs/absent", () => {
    const m = findDuplicateMatch({ name: "J. Smith", email: "JOHN@example.com" }, LIST);
    expect(m).toEqual({ customer: LIST[0], on: "email" });
  });

  it("matches on exact normalized name when no phone/email", () => {
    const m = findDuplicateMatch({ name: "  bob   JONES " }, LIST);
    expect(m).toEqual({ customer: LIST[2], on: "name" });
  });

  it("returns null when nothing matches", () => {
    expect(findDuplicateMatch({ name: "Nobody Here", phone: "5550001111" }, LIST)).toBeNull();
  });

  it("phone confidence wins over a later coincidental name collision", () => {
    const list = [
      { id: 10, name: "Same Name", phone: "5550000000" },
      { id: 11, name: "Different", phone: "5551112222" },
    ];
    // Candidate shares NAME with id 10 but PHONE with id 11 -> phone wins.
    const m = findDuplicateMatch({ name: "Same Name", phone: "555-111-2222" }, list);
    expect(m).toEqual({ customer: list[1], on: "phone" });
  });

  it("excludeId prevents a record matching itself (edit mode)", () => {
    const m = findDuplicateMatch(
      { name: "John Smith", phone: "5551234567" },
      LIST,
      { excludeId: 1 },
    );
    expect(m).toBeNull();
  });

  it("ignores short phone fragments (<7 digits)", () => {
    const m = findDuplicateMatch({ phone: "555" }, [{ id: 9, phone: "555" }]);
    expect(m).toBeNull();
  });

  it("ignores too-short names (<3 chars)", () => {
    const m = findDuplicateMatch({ name: "Jo" }, [{ id: 9, name: "Jo" }]);
    expect(m).toBeNull();
  });

  it("is null-safe on empty/garbage input", () => {
    expect(findDuplicateMatch({}, LIST)).toBeNull();
    expect(findDuplicateMatch({ name: "x" }, null)).toBeNull();
    expect(findDuplicateMatch(null, LIST)).toBeNull();
  });
});

describe("bestLookupTerm", () => {
  it("prefers phone digits when usably long", () => {
    expect(bestLookupTerm({ name: "John", phone: "(555) 123-4567" })).toBe("5551234567");
  });
  it("falls back to email, then name", () => {
    expect(bestLookupTerm({ name: "John", email: "J@X.com" })).toBe("j@x.com");
    expect(bestLookupTerm({ name: "John Smith" })).toBe("john smith");
  });
  it("returns empty string when nothing worth querying", () => {
    expect(bestLookupTerm({ name: "Jo", phone: "55" })).toBe("");
    expect(bestLookupTerm({})).toBe("");
  });
});

describe("lookupTerms", () => {
  it("returns every present identifier, strongest first", () => {
    expect(lookupTerms({ name: "John Smith", phone: "(555) 123-4567", email: "J@X.com" })).toEqual([
      "5551234567",
      "j@x.com",
      "john smith",
    ]);
  });
  it("omits fields that are absent or too short", () => {
    expect(lookupTerms({ name: "John Smith", phone: "55" })).toEqual(["john smith"]);
    expect(lookupTerms({ phone: "5551234567" })).toEqual(["5551234567"]);
  });
  it("returns an empty array when there's nothing worth querying", () => {
    expect(lookupTerms({ name: "Jo", phone: "55" })).toEqual([]);
    expect(lookupTerms({})).toEqual([]);
  });
});
