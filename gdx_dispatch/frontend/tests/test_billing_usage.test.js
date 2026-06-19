import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BillingUsage from "../src/views/BillingUsage.vue";

function mockFetch(responses) {
  const queue = [...responses];
  globalThis.fetch = vi.fn(() => {
    const next = queue.shift() || { ok: true, status: 200, json: async () => ({}) };
    return Promise.resolve({
      ok: next.ok !== false,
      status: next.status || 200,
      json: async () => next.body,
    });
  });
}

// AppLayout pulls in AppSidebar (Pinia store) — stub so these tests stay
// focused on BillingUsage's own behavior.
const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
};
const mountOpts = { global: { stubs } };

describe("BillingUsage.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    globalThis.fetch = undefined;
  });

  it("renders usage rows with plan code + limits", async () => {
    mockFetch([
      {
        body: {
          tenant_id: "t1",
          plan_code: "growth",
          period_kind: "month",
          period_start: "2026-04-01T00:00:00+00:00",
          usage: [
            {
              event_type: "gdx.customer.created.v1",
              quantity: 42,
              limit: 100,
              over_limit: false,
            },
            {
              event_type: "gdx.job.completed.v1",
              quantity: 510,
              limit: 500,
              over_limit: true,
            },
          ],
          overages_this_period: [
            {
              event_type: "gdx.job.completed.v1",
              observed_quantity: 510,
              limit_value: 500,
              detected_at: "2026-04-19T10:00:00+00:00",
            },
          ],
        },
      },
    ]);

    const wrapper = mount(BillingUsage, mountOpts);
    await flushPromises();

    expect(wrapper.get('[data-testid="billing-usage-plan-code"]').text()).toBe("growth");
    expect(wrapper.find('[data-testid="billing-usage-table"]').exists()).toBe(true);
    expect(
      wrapper.get('[data-testid="billing-usage-qty-gdx.customer.created.v1"]').text(),
    ).toBe("42");
    expect(
      wrapper.find('[data-testid="billing-usage-over-gdx.job.completed.v1"]').exists(),
    ).toBe(true);
    expect(
      wrapper.find('[data-testid="billing-usage-overage-gdx.job.completed.v1"]').exists(),
    ).toBe(true);
  });

  it("renders empty state when no metered events", async () => {
    mockFetch([
      {
        body: {
          tenant_id: "t1",
          plan_code: "free",
          period_kind: "month",
          period_start: "2026-04-01T00:00:00+00:00",
          usage: [],
          overages_this_period: [],
        },
      },
    ]);

    const wrapper = mount(BillingUsage, mountOpts);
    await flushPromises();

    expect(wrapper.find('[data-testid="billing-usage-empty"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="billing-usage-overages"]').exists()).toBe(false);
  });

  it("shows error banner on API failure", async () => {
    mockFetch([{ ok: false, status: 500, body: { detail: "boom" } }]);

    const wrapper = mount(BillingUsage, mountOpts);
    await flushPromises();

    expect(wrapper.find('[data-testid="billing-usage-error"]').exists()).toBe(true);
    expect(wrapper.get('[data-testid="billing-usage-error"]').text()).toContain("boom");
  });

  it("reloads on period change", async () => {
    mockFetch([
      {
        body: {
          tenant_id: "t1",
          plan_code: "free",
          period_kind: "month",
          period_start: "2026-04-01T00:00:00+00:00",
          usage: [],
          overages_this_period: [],
        },
      },
      {
        body: {
          tenant_id: "t1",
          plan_code: "free",
          period_kind: "day",
          period_start: "2026-04-19T00:00:00+00:00",
          usage: [
            {
              event_type: "gdx.customer.created.v1",
              quantity: 3,
              limit: null,
              over_limit: false,
            },
          ],
          overages_this_period: [],
        },
      },
    ]);

    const wrapper = mount(BillingUsage, mountOpts);
    await flushPromises();

    const select = wrapper.get('[data-testid="billing-usage-period-select"]');
    await select.setValue("day");
    await flushPromises();

    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    const secondCallUrl = globalThis.fetch.mock.calls[1][0];
    expect(secondCallUrl).toContain("period_kind=day");
    expect(wrapper.get('[data-testid="billing-usage-qty-gdx.customer.created.v1"]').text()).toBe("3");
  });
});
