/**
 * JobStateOverrideDialog — which paths it offers, and where "Other" posts.
 *
 * /api/jobs/{id} overwrites `lifecycle_stage` with the display label
 * ("Complete", "Cancelled") and carries the stored enum in
 * `lifecycle_stage_raw` (routers/jobs.py get_job). The dialog compared the
 * label against 'completed' / 'cancelled', so Un-complete and Reactivate never
 * rendered and "Other" on a cancelled job posted /uncomplete — a 409. Prod
 * audit_logs held zero job_uncompleted / job_reactivated rows (2026-10-04).
 *
 * The job fixtures below are the shape get_job actually returns. A fixture
 * carrying the raw enum in `lifecycle_stage` would pass against the old code
 * and prove nothing.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const postMock = vi.fn();
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: vi.fn(), post: postMock, patch: vi.fn(), del: vi.fn() }),
}));

const COMPLETED = { id: "job-1", title: "Spring swap", status: "Complete",
  lifecycle_stage: "Complete", lifecycle_stage_raw: "completed" };
const CANCELLED = { id: "job-2", title: "Opener install", status: "Cancelled",
  lifecycle_stage: "Cancelled", lifecycle_stage_raw: "cancelled" };

async function mountDialog(job) {
  const { default: Dlg } = await import("../JobStateOverrideDialog.vue");
  const w = mount(Dlg, {
    props: { modelValue: true, job },
    global: {
      stubs: {
        Dialog: {
          props: ["visible", "header"],
          template: '<div v-if="visible"><h2 data-testid="hdr">{{ header }}</h2><slot /><slot name="footer" /></div>',
        },
        Button: {
          props: ["label", "disabled"],
          template: '<button v-bind="$attrs" :disabled="disabled">{{ label }}</button>',
        },
        Textarea: {
          props: ["modelValue"],
          emits: ["update:modelValue"],
          template: '<textarea v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
        },
        Calendar: true, InputText: true,
      },
    },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  postMock.mockReset();
  postMock.mockResolvedValue({ ok: true });
});

describe("JobStateOverrideDialog — paths on a real get_job payload", () => {
  it("a completed job offers Un-complete, not Reactivate", async () => {
    const w = await mountDialog(COMPLETED);
    expect(w.find('[data-testid="path-uncomplete"]').exists()).toBe(true);
    expect(w.find('[data-testid="path-reactivate"]').exists()).toBe(false);
    expect(w.get('[data-testid="hdr"]').text()).toContain("is completed");
  });

  it("a cancelled job offers Reactivate, not Un-complete, and says cancelled", async () => {
    const w = await mountDialog(CANCELLED);
    expect(w.find('[data-testid="path-reactivate"]').exists()).toBe(true);
    expect(w.find('[data-testid="path-uncomplete"]').exists()).toBe(false);
    expect(w.get('[data-testid="hdr"]').text()).toContain("is cancelled");
  });

  it("Un-complete posts /uncomplete with the reason", async () => {
    const w = await mountDialog(COMPLETED);
    await w.get('[data-testid="path-uncomplete"]').trigger("click");
    await w.get('[data-testid="state-override-reason"]').setValue("closed the wrong job");
    await w.get('[data-testid="state-override-apply"]').trigger("click");
    await flushPromises();
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][0]).toBe("/api/jobs/job-1/uncomplete");
    expect(postMock.mock.calls[0][1].reason).toBe("closed the wrong job");
  });

  it("Other on a cancelled job posts /reactivate, not /uncomplete", async () => {
    const w = await mountDialog(CANCELLED);
    await w.get('[data-testid="path-other"]').trigger("click");
    await w.get('[data-testid="state-override-reason"]').setValue("customer called back");
    await w.get('[data-testid="state-override-apply"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[0][0]).toBe("/api/jobs/job-2/reactivate");
    expect(postMock.mock.calls[0][1].reason).toBe("[other] customer called back");
  });

  it("Other on a completed job posts /uncomplete", async () => {
    const w = await mountDialog(COMPLETED);
    await w.get('[data-testid="path-other"]').trigger("click");
    await w.get('[data-testid="state-override-reason"]').setValue("billing asked");
    await w.get('[data-testid="state-override-apply"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[0][0]).toBe("/api/jobs/job-1/uncomplete");
  });

  it("still resolves a payload that sends only the raw enum", async () => {
    const w = await mountDialog({ id: "job-3", title: "x", lifecycle_stage: "cancelled" });
    expect(w.find('[data-testid="path-reactivate"]').exists()).toBe(true);
  });
});
