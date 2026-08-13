/**
 * MobileJobDetailView — logging parts as the job is worked (Doug 2026-08-12:
 * "the only way to add parts used is during the checkout process. we should be
 * able to add them as we are working on the job").
 *
 * The Parts card could only REQUEST a part (an order for the office). Anything
 * installed had to wait for the closeout form, so a tech either carried it in
 * their head all afternoon or typed it into a note — where nothing orders,
 * counts, or bills it.
 *
 * Pinned here:
 *  1. "Used it" writes to the parts-used endpoint (billable spine, status
 *     'used'), NOT to the parts-needed order queue.
 *  2. Installed and requested parts render as separate answers — a part in the
 *     door must never be labelled "Needed".
 *  3. The write is offline-queued like every other write on this screen, and a
 *     queued row says so instead of claiming success.
 *  4. A mis-tap is undoable while unbilled, and NOT undoable once billed —
 *     that row is invoice history.
 */
import "fake-indexeddb/auto";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";

const getMock = vi.fn();
const postQueuedMock = vi.fn();
const delMock = vi.fn();
const toastAdd = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-123" }, query: {}, path: "/mobile/jobs/job-123" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("../../composables/useApi", () => ({
  useApi: () => ({
    get: getMock,
    post: vi.fn(),
    patch: vi.fn(),
    del: delMock,
    postQueued: postQueuedMock,
  }),
}));
vi.mock("../../composables/usePhotoQueue", () => ({
  usePhotoQueue: () => ({
    pendingPhotos: ref(0),
    uploadingPhotos: ref(false),
    capturePhoto: vi.fn(),
    drainPhotos: vi.fn(),
  }),
}));

const stubs = {
  Button: {
    props: ["label", "icon", "loading", "severity", "text", "rounded", "outlined", "disabled"],
    template: '<button v-bind="$attrs" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  InputText: {
    props: ["modelValue"],
    template: '<input v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value); $emit(\'input\', $event)" />',
  },
  InputNumber: {
    props: ["modelValue"],
    template: '<input type="number" v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
  },
  Checkbox: { props: ["modelValue"], template: "<input type=\"checkbox\" />" },
  MobileJobCloseoutDialog: { props: ["visible", "jobId"], template: "<div />" },
  MobileInvoiceDialog: { props: ["visible", "job"], template: "<div />" },
};

function payload(parts = []) {
  return {
    job: {
      id: "job-123",
      title: "Spring replacement",
      dispatch_status: "on_site",
      customer: { id: "c1", name: "Acme" },
    },
    notes: [],
    photos: [],
    parts,
  };
}

async function mountWith(parts = []) {
  const { default: View } = await import("../MobileJobDetailView.vue");
  getMock.mockImplementation(async (url) => {
    if (String(url).includes("/api/catalogs")) return [];
    return payload(parts);
  });
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

async function typePart(w, name) {
  const search = w.find('[data-testid="mjd-part-search"]');
  await search.setValue(name);
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  postQueuedMock.mockResolvedValue({ ok: true });
  delMock.mockResolvedValue({ ok: true });
});

describe("logging a part used mid-job", () => {
  it("posts to parts-used, not to the order queue", async () => {
    const w = await mountWith();
    await typePart(w, "Torsion spring 200x2.0");
    await w.find('[data-testid="mjd-part-used"]').trigger("click");
    await flushPromises();

    expect(postQueuedMock).toHaveBeenCalledTimes(1);
    const [url, body] = postQueuedMock.mock.calls[0];
    expect(url).toBe("/api/mobile/jobs/job-123/parts-used");
    expect(body.parts).toHaveLength(1);
    expect(body.parts[0]).toMatchObject({ name: "Torsion spring 200x2.0", qty: 1 });
    // Free text: no inventory id is invented for a catalog/typed part.
    expect(body.parts[0].part_id).toBeNull();
  });

  it("still requests a part through the order queue", async () => {
    const w = await mountWith();
    await typePart(w, "Bottom bracket");
    await w.find('[data-testid="mjd-part-add"]').trigger("click");
    await flushPromises();

    expect(postQueuedMock.mock.calls[0][0]).toBe("/api/jobs/job-123/parts-needed");
  });

  it("shows a queued log as waiting for signal, not as saved", async () => {
    postQueuedMock.mockResolvedValue({ queued: true, idempotency_key: "k1" });
    const w = await mountWith();
    await typePart(w, "Roller");
    await w.find('[data-testid="mjd-part-used"]').trigger("click");
    await flushPromises();

    const used = w.find('[data-testid="mjd-used-list"]');
    expect(used.exists()).toBe(true);
    expect(used.text()).toContain("Roller");
    expect(used.text()).toContain("waiting for signal");
  });
});

describe("the Parts card answers two questions", () => {
  it("separates what was installed from what was ordered", async () => {
    const w = await mountWith([
      { id: "r1", part_name: "Cable", quantity: 2, status: "needed", source: "request" },
      { id: "u1", part_name: "Spring", quantity: 1, status: "used", source: "mobile" },
    ]);

    const used = w.find('[data-testid="mjd-used-list"]');
    const requested = w.find('[data-testid="mjd-part-list"]');
    expect(used.text()).toContain("Spring");
    expect(used.text()).not.toContain("Cable");
    expect(requested.text()).toContain("Cable");
    expect(requested.text()).not.toContain("Spring");
  });
});

describe("undo", () => {
  it("removes an unbilled logged part", async () => {
    const w = await mountWith([
      { id: "u1", part_name: "Spring", quantity: 1, status: "used", source: "mobile" },
    ]);
    await w.find('[data-testid="mjd-part-undo"]').trigger("click");
    await flushPromises();

    expect(delMock).toHaveBeenCalledWith("/api/mobile/jobs/job-123/parts-used/u1");
  });

  it("offers no undo once the office has billed it", async () => {
    const w = await mountWith([
      {
        id: "u1",
        part_name: "Spring",
        quantity: 1,
        status: "used",
        source: "mobile",
        billed_invoice_id: "inv-1",
      },
    ]);
    expect(w.find('[data-testid="mjd-part-undo"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-used-list"]').text()).toContain("billed");
  });
});
