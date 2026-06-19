// SS-28 slice F — AuditLogViewer.vue component tests.
//
// Stubs fetchFn so no HTTP actually fires. Verifies:
//   * on mount, renders rows from the fetched payload
//   * chain-integrity badge flips red when valid=false
//   * filter form submit re-issues the query with params
//   * empty result shows the empty-state row
//   * next/prev pagination updates offset

import { describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import AuditLogViewer from "../src/views/AuditLogViewer.vue";

const sampleRows = [
  {
    id: "r1",
    created_at: "2026-04-19T10:00:00",
    action: "GET /api/jobs",
    resource_type: "http.request",
    resource_id: "/api/jobs",
    principal_identity_id: "p1",
    result: "ok",
    ip_address: "10.0.0.1",
    user_agent: "ua",
  },
  {
    id: "r2",
    created_at: "2026-04-19T10:01:00",
    action: "POST /api/jobs",
    resource_type: "http.request",
    resource_id: "/api/jobs",
    principal_identity_id: "p1",
    result: "denied",
    ip_address: "10.0.0.1",
    user_agent: "ua",
  },
];

// AppLayout pulls in AppSidebar which uses Pinia; stub so tests stay focused
// on AuditLogViewer's own behavior (added 2026-05-09 when the view was
// wrapped in AppLayout for layout consistency).
const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
};

function mountWithFetch(fetchFn) {
  return mount(AuditLogViewer, {
    props: { fetchFn },
    global: { stubs },
  });
}

describe("AuditLogViewer", () => {
  it("renders rows returned by fetchFn", async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      total: 2,
      offset: 0,
      limit: 50,
      rows: sampleRows,
      chain_integrity: { valid: true, break_at: -1 },
    });
    const w = mountWithFetch(fetchFn);
    await flushPromises();

    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(w.find('[data-testid="audit-row-r1"]').exists()).toBe(true);
    expect(w.find('[data-testid="audit-row-r2"]').exists()).toBe(true);
    expect(w.text()).toContain("GET /api/jobs");
  });

  it("shows chain-intact badge when valid", async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      total: 0,
      offset: 0,
      limit: 50,
      rows: [],
      chain_integrity: { valid: true, break_at: -1 },
    });
    const w = mountWithFetch(fetchFn);
    await flushPromises();
    const badge = w.find('[data-testid="chain-integrity-badge"]');
    expect(badge.exists()).toBe(true);
    expect(badge.classes()).toContain("chain-ok");
    expect(badge.text()).toContain("Chain intact");
  });

  it("shows chain-broken badge when invalid", async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      total: 0,
      offset: 0,
      limit: 50,
      rows: [],
      chain_integrity: { valid: false, break_at: 3 },
    });
    const w = mountWithFetch(fetchFn);
    await flushPromises();
    const badge = w.find('[data-testid="chain-integrity-badge"]');
    expect(badge.classes()).toContain("chain-broken");
    expect(badge.text()).toContain("BROKEN");
    expect(badge.text()).toContain("3");
  });

  it("empty rows shows empty-state row", async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      total: 0,
      offset: 0,
      limit: 50,
      rows: [],
      chain_integrity: { valid: true, break_at: -1 },
    });
    const w = mountWithFetch(fetchFn);
    await flushPromises();
    expect(w.find('[data-testid="empty-state"]').exists()).toBe(true);
  });

  it("filter form submit re-calls fetchFn with query params", async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      total: 0,
      offset: 0,
      limit: 50,
      rows: [],
      chain_integrity: { valid: true, break_at: -1 },
    });
    const w = mountWithFetch(fetchFn);
    await flushPromises();
    fetchFn.mockClear();

    await w.find('[data-testid="filter-action"]').setValue("api.call");
    await w.find('[data-testid="audit-filters"]').trigger("submit.prevent");
    await flushPromises();

    expect(fetchFn).toHaveBeenCalledTimes(1);
    const url = fetchFn.mock.calls[0][0];
    expect(url).toContain("action=api.call");
  });

  it("next/prev pagination updates offset", async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      total: 150,
      offset: 0,
      limit: 50,
      rows: sampleRows,
      chain_integrity: { valid: true, break_at: -1 },
    });
    const w = mountWithFetch(fetchFn);
    await flushPromises();
    fetchFn.mockClear();

    await w.find('[data-testid="next-page"]').trigger("click");
    await flushPromises();

    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0][0]).toContain("offset=50");

    await w.find('[data-testid="prev-page"]').trigger("click");
    await flushPromises();
    expect(fetchFn).toHaveBeenCalledTimes(2);
    expect(fetchFn.mock.calls[1][0]).toContain("offset=0");
  });
});
