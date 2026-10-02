/**
 * OutstandingAgingDialog — the Total Outstanding card's age breakdown.
 *
 * Pinned contract:
 *  - Rows come from the invoices the page passed in and add up to the total.
 *  - Typing new day ranges re-cuts the rows.
 *  - A card total that disagrees with the loaded list is called out, not hidden.
 */
import { describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import PrimeVue from "primevue/config";

vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import OutstandingAgingDialog from "../OutstandingAgingDialog.vue";

function daysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const invoices = [
  { id: "a", invoice_number: "INV-1", status: "Sent", total: 100, balance_due: 100, invoice_date: daysAgo(5) },
  { id: "b", invoice_number: "INV-2", status: "Overdue", total: 250.5, balance_due: 250.5, invoice_date: daysAgo(45) },
  { id: "c", invoice_number: "INV-3", status: "Paid", total: 999, balance_due: 0, invoice_date: daysAgo(10) },
];

function mountDialog(props = {}) {
  return mount(OutstandingAgingDialog, {
    props: { visible: true, invoices, cardTotal: 350.5, ...props },
    global: {
      plugins: [PrimeVue],
      stubs: { Dialog: { props: ["visible"], template: '<div v-if="visible"><slot /></div>' } },
    },
  });
}

describe("OutstandingAgingDialog", () => {
  it("buckets the receivables and totals to the card", async () => {
    const w = mountDialog();
    await flushPromises();
    expect(w.get('[data-testid="aging-row-d0_30"]').text()).toBe("$100.00");
    expect(w.get('[data-testid="aging-row-d31_60"]').text()).toBe("$250.50");
    expect(w.get('[data-testid="aging-total"]').text()).toContain("$350.50");
    expect(w.find('[data-testid="aging-mismatch"]').exists()).toBe(false);
  });

  it("re-cuts when the day ranges change", async () => {
    const w = mountDialog();
    await w.get('[data-testid="aging-breakpoints"]').setValue("60");
    expect(w.get('[data-testid="aging-row-d0_60"]').text()).toBe("$350.50");
    expect(w.get('[data-testid="aging-row-d61_plus"]').text()).toBe("$0.00");
  });

  it("calls out a card total the loaded list does not reach", async () => {
    const w = mountDialog({ cardTotal: 500 });
    expect(w.get('[data-testid="aging-mismatch"]').text()).toContain("$500.00");
  });
});

describe("OutstandingAgingDialog — remembered ranges", () => {
  it("reopens with the ranges the office last typed", async () => {
    localStorage.removeItem("gdx_billing_aging");
    const first = mountDialog();
    await first.get('[data-testid="aging-breakpoints"]').setValue("15, 45");
    await flushPromises();
    first.unmount();
    const second = mountDialog();
    expect(second.get('[data-testid="aging-breakpoints"]').element.value).toBe("15, 45");
    expect(second.find('[data-testid="aging-row-d0_15"]').exists()).toBe(true);
    localStorage.removeItem("gdx_billing_aging");
  });
});

describe("OutstandingAgingDialog — date-filtered card", () => {
  it("says the breakdown follows the page's date filter", () => {
    const w = mountDialog({ scopeNote: "Only invoices issued in the page's date filter (This year), matching the card." });
    expect(w.get('[data-testid="aging-scope"]').text()).toContain("This year");
  });

  it("warns when the rows it was given fall short of or exceed a filtered card", () => {
    // Dialog side only: BillingView's wiring (kpiWindowInvoices + the shown
    // total) has no mount test — it was proven in a browser instead.
    const w = mountDialog({ cardTotal: 100 });
    expect(w.get('[data-testid="aging-mismatch"]').text()).toContain("$100.00");
  });
});
