import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the auth store so the composable's pre-auth bail-out doesn't fire.
vi.mock("../src/stores/auth", () => ({
  useAuthStore: () => ({ isAuthenticated: true }),
}));

const getMock = vi.fn();
vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), request: vi.fn() }),
}));

const { useTenantModules } = await import("../src/composables/useTenantModules");

describe("useTenantModules — D101 sidebar visibility (an earlier session, 2026-04-25)", () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  afterEach(() => {
    getMock.mockReset();
  });

  it("respects per-module enabled=false from the server response array", async () => {
    // Server shape: { tenant_tier, modules: [{key, enabled, ...}, ...] }.
    // Pre-fix this branch keyed by array index ("0"/"1"/...) so disabled
    // modules still showed in the sidebar.
    getMock.mockResolvedValueOnce({
      tenant_tier: "professional",
      modules: [
        { key: "jobs", name: "Jobs", tier: "starter", enabled: true, locked: false },
        { key: "warranties", name: "Warranty Tracking", tier: "professional", enabled: false, locked: false },
        { key: "quickbooks", name: "QuickBooks Sync", tier: "professional", enabled: true, locked: false },
      ],
    });
    const { loadTenantModules, isEnabled, enabledModules } = useTenantModules();
    await loadTenantModules();

    expect(isEnabled("jobs")).toBe(true);
    expect(isEnabled("warranties")).toBe(false);
    expect(isEnabled("quickbooks")).toBe(true);
    // Sanity: enabledModules keys aren't '0', '1', '2'.
    expect(Object.keys(enabledModules.value)).not.toContain("0");
    expect(Object.keys(enabledModules.value)).not.toContain("1");
  });

  it("falls back to defaults when API call fails", async () => {
    getMock.mockRejectedValueOnce(new Error("network"));
    const { loadTenantModules, isEnabled } = useTenantModules();
    await loadTenantModules();
    // Defaults: jobs enabled.
    expect(isEnabled("jobs")).toBe(true);
  });
});
