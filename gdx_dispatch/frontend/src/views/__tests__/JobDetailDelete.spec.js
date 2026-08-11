/**
 * JobDetailView — deleting the job you are looking at.
 *
 * Doug 2026-08-11: "there is no way of deleting a job when in the job page."
 * He was right. DELETE /api/jobs/{id} has existed since the initial public
 * release, but the only two UIs that called it were the Jobs LIST row actions
 * (a trash icon in the far-right Actions column, past seven other columns) and
 * the Ready-for-Billing queue. Open the job itself and every verb was there
 * except the one that removes it — Edit, Complete, Create Invoice, Re-open,
 * Create Estimate, Install Sheet, and three sub-item deletes for diagnosis /
 * hazard / receipt, which made the absence read as deliberate.
 *
 * Pinned here:
 *  1. The verb exists on the job page at all — the whole point.
 *  2. It is confirmed before it fires. This file's confirm path is
 *     useDestructiveConfirm, which auto-accepted silently for months
 *     (issue #215, fixed in PR #280); a regression there turns a stray
 *     click into a deleted job with no dialog.
 *  3. Cancel deletes nothing.
 *  4. Live invoices are NAMED in the confirm. The endpoint soft-deletes the
 *     job and cascades only to the mirrored appointment — invoices and
 *     estimates survive, so deleting a job with a live invoice orphans real
 *     money in Billing. The office has to be told before it happens, not after.
 *  5. Technicians don't get the verb (same `patchable` gate as assignment
 *     edits). The API allows it — technicians carry jobs.write for mobile
 *     job-create — so hiding it is the only thing standing between a tech
 *     and a deleted job.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const apiGet = vi.fn();
const apiDel = vi.fn();
const routerPush = vi.fn();
const confirmAsync = vi.fn();
let userRole = "admin";

vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { id: "job-123" }, query: {}, path: "/jobs/job-123" }),
  useRouter: () => ({ push: routerPush, back: vi.fn(), replace: vi.fn() }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({
    get: apiGet,
    del: apiDel,
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
  }),
}));
vi.mock("../../composables/useDestructiveConfirm", () => ({
  useDestructiveConfirm: () => ({ confirmAsync, confirmDestructive: vi.fn() }),
}));
vi.mock("../../stores/auth", () => ({
  useAuthStore: () => ({ user: { role: userRole }, hasPermission: () => true }),
}));

const stubs = {
  Button: {
    props: ["label", "icon", "severity", "text", "rounded", "outlined", "disabled", "loading"],
    // `emits` matters: without it Vue leaves onClick in $attrs, v-bind
    // re-attaches it alongside the $emit, and every click counts twice —
    // which reads as a double-fire bug in the view rather than the stub.
    emits: ["click"],
    template: '<button v-bind="$attrs" @click="$emit(\'click\')">{{ label }}</button>',
  },
};

const JOB = {
  id: "job-123",
  job_number: "1042",
  title: "Broken spring — 2 car",
  status: "Scheduled",
  lifecycle_stage: "scheduled",
  priority: "Normal",
};

function seedApi({ invoices = [] } = {}) {
  apiGet.mockImplementation((url) => {
    if (url === "/api/jobs/job-123") return Promise.resolve(JOB);
    if (url.includes("/invoices")) return Promise.resolve(invoices);
    return Promise.resolve([]);
  });
}

async function mountView() {
  const JobDetailView = (await import("../JobDetailView.vue")).default;
  const wrapper = mount(JobDetailView, {
    shallow: true,
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return wrapper;
}

const deleteBtn = (w) => w.find('[data-testid="job-detail-delete"]');

describe("JobDetailView — delete the job from the job page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    userRole = "admin";
    seedApi();
    apiDel.mockResolvedValue({ ok: true });
    confirmAsync.mockResolvedValue(true);
  });

  it("offers Delete on the job page", async () => {
    const w = await mountView();
    expect(deleteBtn(w).exists()).toBe(true);
  });

  it("confirms first, then soft-deletes and returns to the list", async () => {
    const w = await mountView();
    await deleteBtn(w).trigger("click");
    await flushPromises();

    expect(confirmAsync).toHaveBeenCalledTimes(1);
    expect(apiDel).toHaveBeenCalledWith("/api/jobs/job-123", expect.anything());
    expect(routerPush).toHaveBeenCalledWith("/jobs");
  });

  it("names the job in the confirm so the wrong tab is survivable", async () => {
    const w = await mountView();
    await deleteBtn(w).trigger("click");
    await flushPromises();

    const opts = confirmAsync.mock.calls[0][0];
    expect(opts.message).toContain("1042");
    expect(opts.message).toContain("Broken spring — 2 car");
  });

  it("deletes nothing when the confirm is cancelled", async () => {
    confirmAsync.mockResolvedValue(false);
    const w = await mountView();
    await deleteBtn(w).trigger("click");
    await flushPromises();

    expect(apiDel).not.toHaveBeenCalled();
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("warns by invoice number when live invoices would be orphaned", async () => {
    seedApi({
      invoices: [
        { id: "i1", invoice_number: "INV-000412", status: "sent" },
        { id: "i2", invoice_number: "INV-000418", status: "draft" },
        { id: "i3", invoice_number: "INV-000399", status: "void" },
      ],
    });
    const w = await mountView();
    await deleteBtn(w).trigger("click");
    await flushPromises();

    const { message } = confirmAsync.mock.calls[0][0];
    expect(message).toContain("INV-000412");
    expect(message).toContain("INV-000418");
    // Void is dead money — the strip hides it, so the warning must too.
    expect(message).not.toContain("INV-000399");
  });

  it("says nothing about invoices when the job carries none", async () => {
    const w = await mountView();
    await deleteBtn(w).trigger("click");
    await flushPromises();

    expect(confirmAsync.mock.calls[0][0].message).not.toContain("Billing");
  });

  it("does not offer the verb to a technician", async () => {
    userRole = "technician";
    const w = await mountView();
    expect(deleteBtn(w).exists()).toBe(false);
  });

  it("stays on the job when the delete fails", async () => {
    apiDel.mockRejectedValue(new Error("boom"));
    const w = await mountView();
    await deleteBtn(w).trigger("click");
    await flushPromises();

    expect(routerPush).not.toHaveBeenCalled();
  });
});
