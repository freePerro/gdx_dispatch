import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { ref, effectScope, nextTick } from "vue";
import { useListPrefs } from "../useListPrefs";

const SCHEMA = {
  activeStatus: { default: "All", valid: (v) => ["All", "Scheduled", "Complete"].includes(v) },
  searchQuery: { default: "", valid: (v) => typeof v === "string" },
};

// Run the composable inside an effect scope so its internal watch() is
// contained and disposed between tests (no leaking reactive subscriptions).
function run(namespace, refs) {
  const scope = effectScope();
  let result;
  scope.run(() => {
    result = useListPrefs(namespace, refs, SCHEMA);
  });
  return { scope, result };
}

describe("useListPrefs", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("leaves refs at their defaults when nothing is stored", () => {
    const activeStatus = ref("All");
    const searchQuery = ref("");
    const { scope } = run("jobs", { activeStatus, searchQuery });
    expect(activeStatus.value).toBe("All");
    expect(searchQuery.value).toBe("");
    scope.stop();
  });

  it("loads stored values into the refs on init", () => {
    localStorage.setItem(
      "gdx.listprefs.jobs",
      JSON.stringify({ activeStatus: "Scheduled", searchQuery: "smith" }),
    );
    const activeStatus = ref("All");
    const searchQuery = ref("");
    const { scope } = run("jobs", { activeStatus, searchQuery });
    expect(activeStatus.value).toBe("Scheduled");
    expect(searchQuery.value).toBe("smith");
    scope.stop();
  });

  it("discards an invalid stored status and uses the default", () => {
    localStorage.setItem(
      "gdx.listprefs.jobs",
      JSON.stringify({ activeStatus: "Archived", searchQuery: "x" }),
    );
    const activeStatus = ref("All");
    const searchQuery = ref("");
    const { scope } = run("jobs", { activeStatus, searchQuery });
    expect(activeStatus.value).toBe("All");
    expect(searchQuery.value).toBe("x");
    scope.stop();
  });

  it("persists ref changes back to localStorage", async () => {
    const activeStatus = ref("All");
    const searchQuery = ref("");
    const { scope } = run("jobs", { activeStatus, searchQuery });

    activeStatus.value = "Complete";
    searchQuery.value = "garage";
    await nextTick();

    const stored = JSON.parse(localStorage.getItem("gdx.listprefs.jobs"));
    expect(stored).toEqual({ activeStatus: "Complete", searchQuery: "garage" });
    scope.stop();
  });

  it("namespaces storage per view (no cross-talk between jobs and billing)", async () => {
    const jobsStatus = ref("All");
    const billStatus = ref("All");
    const s1 = run("jobs", { activeStatus: jobsStatus, searchQuery: ref("") });
    const s2 = run("billing", { activeStatus: billStatus, searchQuery: ref("") });

    jobsStatus.value = "Scheduled";
    billStatus.value = "Complete";
    await nextTick();

    expect(JSON.parse(localStorage.getItem("gdx.listprefs.jobs")).activeStatus).toBe("Scheduled");
    expect(JSON.parse(localStorage.getItem("gdx.listprefs.billing")).activeStatus).toBe("Complete");
    s1.scope.stop();
    s2.scope.stop();
  });

  it("round-trips: a change in one mount is restored in the next mount", async () => {
    const a1 = ref("All");
    const s1 = run("jobs", { activeStatus: a1, searchQuery: ref("") });
    a1.value = "Scheduled";
    await nextTick();
    s1.scope.stop();

    // Fresh mount reading the same storage
    const a2 = ref("All");
    const s2 = run("jobs", { activeStatus: a2, searchQuery: ref("") });
    expect(a2.value).toBe("Scheduled");
    s2.scope.stop();
  });

  describe("storage unavailable (private mode / disabled)", () => {
    let getSpy;
    let setSpy;
    afterEach(() => {
      getSpy?.mockRestore();
      setSpy?.mockRestore();
    });

    it("falls back to defaults when getItem throws and does not crash", () => {
      getSpy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
        throw new Error("blocked");
      });
      const activeStatus = ref("All");
      const { scope } = run("jobs", { activeStatus, searchQuery: ref("") });
      expect(activeStatus.value).toBe("All");
      scope.stop();
    });

    it("silently no-ops when setItem throws (state stays in memory)", async () => {
      setSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new Error("quota");
      });
      const activeStatus = ref("All");
      const { scope } = run("jobs", { activeStatus, searchQuery: ref("") });
      expect(() => {
        activeStatus.value = "Complete";
      }).not.toThrow();
      await nextTick();
      expect(activeStatus.value).toBe("Complete");
      scope.stop();
    });
  });
});
