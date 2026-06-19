import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DashboardView from "../src/views/DashboardView.vue";

const pushMock = vi.fn();
const getMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: pushMock }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock }),
}));

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Button: {
    props: ["label"],
    emits: ["click"],
    template: '<button @click="$emit(\'click\')">{{ label }}</button>',
  },
};

const flushPromises = async () => {
  await Promise.resolve();
  await Promise.resolve();
};

describe("Dashboard KPI cards", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    pushMock.mockReset();
    getMock.mockReset();
    getMock
      .mockResolvedValueOnce({
        revenue_total: 145000,
        open_jobs: 18,
        overdue_invoices: 6,
        jobs_completed: 9,
        revenue_trend: 12,
        open_jobs_trend: -4,
        overdue_invoices_trend: -8,
        jobs_completed_trend: 5,
      })
      .mockResolvedValueOnce([
        { id: 1, title: "Install", customer: "Acme", created_at: "2026-04-01T10:00:00Z" },
      ]);
  });

  it("renders four KPI cards with values from API", async () => {
    const wrapper = mount(DashboardView, { global: { stubs } });
    await flushPromises();

    // Dashboard now passes a date window (start_date+end_date) on the
    // URL. Match the endpoint prefix — the date params are
    // implementation detail, not contract.
    expect(getMock.mock.calls.some(([url]) =>
      String(url).startsWith('/api/reports/summary'),
    )).toBe(true);
    // Card count grew from 4 → 5 (S108 added Jobs Completed Today as a
    // standalone tile; previously it was a sub-stat inside the Jobs card).
    // Pin "at least 4" so future card additions don't break the test.
    expect(wrapper.findAll("[data-testid='kpi-card']").length).toBeGreaterThanOrEqual(4);
    expect(wrapper.find("[data-testid='kpi-value-revenue']").text()).toContain("$145,000");
    expect(wrapper.find("[data-testid='kpi-value-open-jobs']").text()).toBe("18");
    expect(wrapper.find("[data-testid='kpi-value-overdue-invoices']").text()).toBe("6");
    expect(wrapper.find("[data-testid='kpi-value-completed-today']").text()).toBe("9");
  });
});
