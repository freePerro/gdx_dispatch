/**
 * Re-open dialog — the R0 question (multi-day jobs plan §5.2a).
 *
 * /uncomplete and /reactivate answer 409 needs_answer, question
 * rebook_closed_day, {tech_ids, day}, when a crew tech already holds a closed
 * visit on the new day (routers/jobs.py _visit_refused, services/visit_sync
 * _book). The dialog must ask the plan's question with the tech's NAME and a
 * readable day, and resend with rebook_closed_day true or false — not show the
 * raw 409. status_only_arrival points at the Appointments page.
 *
 * The 409 is thrown the way useApi throws it: message = detail, body = JSON.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const postMock = vi.fn();
const getMock = vi.fn();
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: postMock, patch: vi.fn(), del: vi.fn() }),
}));

const COMPLETED = { id: "job-1", title: "Spring swap", status: "Complete",
  lifecycle_stage: "Complete", lifecycle_stage_raw: "completed" };
const CANCELLED = { id: "job-2", title: "Opener install", status: "Cancelled",
  lifecycle_stage: "Cancelled", lifecycle_stage_raw: "cancelled" };

function apiError(status, body) {
  return Object.assign(new Error(body.detail), { status, body });
}

const REBOOK_409 = apiError(409, {
  detail: "Already has a closed visit on 2026-10-06. Book that day again?",
  code: "needs_answer", question: "rebook_closed_day",
  tech_ids: ["tech-mike"], day: "2026-10-06",
});

async function mountDialog(job, technicians = [{ id: "tech-mike", name: "Mike" }]) {
  const { default: Dlg } = await import("../JobStateOverrideDialog.vue");
  const w = mount(Dlg, {
    props: { modelValue: true, job, technicians },
    global: {
      stubs: {
        Dialog: {
          props: ["visible", "header"],
          template: '<div v-if="visible"><slot /><slot name="footer" /></div>',
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
        RouterLink: { props: ["to"], template: '<a :href="to" v-bind="$attrs"><slot /></a>' },
        Calendar: true, InputText: true,
      },
    },
  });
  await flushPromises();
  return w;
}

async function uncomplete(w) {
  await w.get('[data-testid="path-uncomplete"]').trigger("click");
  await w.get('[data-testid="state-override-reason"]').setValue("closed the wrong job");
  await w.get('[data-testid="state-override-apply"]').trigger("click");
  await flushPromises();
}

beforeEach(() => {
  postMock.mockReset();
  getMock.mockReset();
});

describe("Re-open dialog — rebook_closed_day", () => {
  it("asks the plan's question with the tech's name and the day", async () => {
    postMock.mockRejectedValueOnce(REBOOK_409);
    const w = await mountDialog(COMPLETED);
    await uncomplete(w);
    const q = w.get('[data-testid="rebook-question"]').text();
    expect(q).toMatch(/^Mike already has a closed visit on \w{3} 10\/6\. Book them on that day again\?/);
    expect(q).toContain("their tap will find no visit");
    // Asked, not shown as a failure.
    expect(w.find('[data-testid="state-override-error"]').exists()).toBe(false);
    expect(w.emitted("applied")).toBeUndefined();
  });

  it("first request carries no answer; Yes resends rebook_closed_day: true", async () => {
    postMock.mockRejectedValueOnce(REBOOK_409).mockResolvedValueOnce({ ok: true });
    const w = await mountDialog(COMPLETED);
    await uncomplete(w);
    expect("rebook_closed_day" in postMock.mock.calls[0][1]).toBe(false);
    await w.get('[data-testid="rebook-yes"]').trigger("click");
    await flushPromises();
    expect(postMock).toHaveBeenCalledTimes(2);
    expect(postMock.mock.calls[1][0]).toBe("/api/jobs/job-1/uncomplete");
    expect(postMock.mock.calls[1][1].rebook_closed_day).toBe(true);
    expect(postMock.mock.calls[1][1].reason).toBe("closed the wrong job");
    expect(w.emitted("applied")).toHaveLength(1);
  });

  it("No resends rebook_closed_day: false on /reactivate", async () => {
    postMock.mockRejectedValueOnce(REBOOK_409).mockResolvedValueOnce({ ok: true });
    const w = await mountDialog(CANCELLED);
    await w.get('[data-testid="path-reactivate"]').trigger("click");
    await w.get('[data-testid="state-override-reason"]').setValue("cancelled in error");
    await w.get('[data-testid="state-override-apply"]').trigger("click");
    await flushPromises();
    await w.get('[data-testid="rebook-no"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[1][0]).toBe("/api/jobs/job-2/reactivate");
    expect(postMock.mock.calls[1][1].rebook_closed_day).toBe(false);
  });

  it("names two techs, fetching names it was not given", async () => {
    postMock.mockRejectedValueOnce(apiError(409, {
      detail: "x", code: "needs_answer", question: "rebook_closed_day",
      tech_ids: ["tech-mike", "tech-sam"], day: "2026-10-06",
    }));
    getMock.mockResolvedValueOnce([{ id: "tech-mike", name: "Mike" }, { id: "tech-sam", name: "Sam" }]);
    const w = await mountDialog(COMPLETED, []);
    await uncomplete(w);
    expect(getMock).toHaveBeenCalledWith("/api/technicians", expect.anything());
    expect(w.get('[data-testid="rebook-question"]').text()).toContain("Mike and Sam already have a closed visit");
  });

  it("status_only_arrival shows the backend sentence and links the Appointments page", async () => {
    postMock.mockRejectedValueOnce(apiError(409, {
      detail: "A visit is marked arrived with no time recorded. On the Appointments page, enter its arrival time or undo the arrival, then try again.",
      code: "needs_answer", question: "status_only_arrival", visit_ids: ["v1"],
    }));
    const w = await mountDialog(CANCELLED);
    await w.get('[data-testid="path-reactivate"]').trigger("click");
    await w.get('[data-testid="state-override-reason"]').setValue("cancelled in error");
    await w.get('[data-testid="state-override-apply"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-testid="state-override-error"]').text()).toContain("marked arrived with no time recorded");
    expect(w.get('[data-testid="state-override-appointments"]').attributes("href")).toBe("/appointments");
    expect(w.find('[data-testid="rebook-question"]').exists()).toBe(false);
  });

  it("an unrelated failure shows its message with no Appointments link", async () => {
    postMock.mockRejectedValueOnce(apiError(409, { detail: "only completed jobs can be un-completed" }));
    const w = await mountDialog(COMPLETED);
    await uncomplete(w);
    expect(w.get('[data-testid="state-override-error"]').text()).toContain("only completed jobs can be un-completed");
    expect(w.find('[data-testid="state-override-appointments"]').exists()).toBe(false);
  });
});
