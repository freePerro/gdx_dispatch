/**
 * MobileSummaryView — "Tomorrow's first stop" shows the JOBSITE (D2).
 *
 * Post-code audit 2026-08-18 §5: this screen reproduced the Jobs-tab bug —
 * `site_address || customer_address` showed the customer HQ for a bound
 * site with no address. Pinned: the missing flag blocks the HQ fallback.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: getMock }),
}));

import MobileSummaryView from "../MobileSummaryView.vue";

const stubs = {
  Button: { template: "<button />" },
  DatePicker: { props: ["modelValue"], template: "<input />" },
  Tag: { props: ["value", "severity"], template: "<span>{{ value }}</span>" },
};

function summaryPayload(nextStop) {
  return {
    date: "2026-08-18",
    jobs_completed: [],
    jobs_completed_count: 0,
    labor_hours: 0,
    parts_requested_count: 0,
    invoices_count: 0,
    revenue_invoiced: 0,
    next_first_stop: nextStop,
  };
}

beforeEach(() => {
  getMock.mockReset();
});

describe("tomorrow's first stop address", () => {
  it("prefers the jobsite over the customer address", async () => {
    getMock.mockResolvedValue(summaryPayload({
      id: "j1", title: "Install", scheduled_at: null,
      customer_name: "Acme", customer_address: "100 Billing Rd",
      site_address: "9 Dock St", site_address_missing: false,
    }));
    const w = mount(MobileSummaryView, { global: { stubs } });
    await flushPromises();
    expect(w.text()).toContain("9 Dock St");
    expect(w.text()).not.toContain("100 Billing Rd");
  });

  it("bound site with NO address says ask-dispatch, never the HQ (D2)", async () => {
    getMock.mockResolvedValue(summaryPayload({
      id: "j1", title: "Install", scheduled_at: null,
      customer_name: "Acme", customer_address: "100 Billing Rd",
      site_address: null, site_address_missing: true,
    }));
    const w = mount(MobileSummaryView, { global: { stubs } });
    await flushPromises();
    expect(w.text()).toContain("No address — ask dispatch");
    expect(w.text()).not.toContain("100 Billing Rd");
  });

  it("payloads predating the fields still show the customer address", async () => {
    getMock.mockResolvedValue(summaryPayload({
      id: "j1", title: "Install", scheduled_at: null,
      customer_name: "Acme", customer_address: "100 Billing Rd",
    }));
    const w = mount(MobileSummaryView, { global: { stubs } });
    await flushPromises();
    expect(w.text()).toContain("100 Billing Rd");
  });
});
