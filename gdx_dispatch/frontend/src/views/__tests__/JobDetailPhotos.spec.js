/**
 * The office job page's Photos tab (Doug 2026-08-12: "a tech adds a photo to a
 * job and can see it in mobile and the office cannot see it in photos").
 *
 * The tab filtered `/api/documents?job_id=` rows on `doc.entity_type ===
 * "job_photo"` — a field DocumentOut has never serialized — so it rendered
 * "No photos yet" for every photo from every source since it shipped. Prod had
 * 31 live photos at the time, all correctly stored.
 *
 * Pinned here:
 *  1. Photos come from job_photos (GET /api/jobs/{id}/photos), the record every
 *     other photo surface reads — not from the documents list.
 *  2. The photo is rendered as an IMAGE, not an emoji and a filename.
 *  3. "No photos", "couldn't load" and "not allowed" are three different
 *     states. The whole bug was one of them impersonating another.
 *  4. Uploading refetches PHOTOS — refetching documents was how the office
 *     watched its own upload vanish.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const requestMock = vi.fn();
const patchMock = vi.fn();
const toastAdd = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-1" }, query: {}, path: "/jobs/job-1" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({
    get: getMock,
    post: vi.fn(),
    patch: patchMock,
    del: vi.fn(),
    request: requestMock,
  }),
}));
vi.mock("../../composables/useDestructiveConfirm", () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(), confirmDestructive: vi.fn() }),
}));
vi.mock("../../stores/auth", () => ({
  useAuthStore: () => ({ user: { role: "admin" }, hasPermission: () => true }),
}));

const PHOTO = {
  id: "photo-1",
  url: "/api/documents/doc-1/download",
  kind: "after",
  caption: "New spring installed",
  uploaded_by: "tech@example.com",
  uploaded_at: "2026-08-12T10:00:00Z",
};

/** Route every endpoint the page loads; only the photo call is interesting. */
function routeGet({ photos = [PHOTO], photosError = null } = {}) {
  getMock.mockImplementation(async (url) => {
    if (String(url).includes("/photos")) {
      if (photosError) throw photosError;
      return photos;
    }
    if (String(url).startsWith("/api/jobs/job-1")) {
      return { id: "job-1", title: "Spring replacement", status: "Complete" };
    }
    return [];
  });
}

async function mountView() {
  const { default: View } = await import("../JobDetailView.vue");
  const w = mount(View, {
    global: {
      stubs: {
        Button: { props: ["label"], template: '<button v-bind="$attrs">{{ label }}</button>' },
        Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
        AuthedImage: {
          props: ["src", "alt"],
          template: '<img :src="src" :alt="alt" data-testid="authed-img" />',
        },
        DataTable: true, Column: true, Dialog: true, Select: true, InputText: true,
        Textarea: true, DatePicker: true, FileUpload: true, ProgressSpinner: true,
        Tabs: true, TabList: true, Tab: true, Message: true, Checkbox: true,
        JobStateChip: true, JobStateOverrideDialog: true, CatalogPickerDialog: true,
        DoorSpecList: true, PhoneInput: true, EmailTimeline: true, InputNumber: true,
      },
    },
  });
  await flushPromises();
  return w;
}

/** The tab body only renders for the active tab. */
async function openPhotosTab(w) {
  w.vm.activeTab = "photos";
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  requestMock.mockResolvedValue({});
  patchMock.mockResolvedValue({});
});

describe("office job page — Photos tab", () => {
  it("reads job_photos, not the documents list", async () => {
    routeGet();
    await mountView();

    const photoCalls = getMock.mock.calls.filter(([u]) => String(u).includes("/photos"));
    expect(photoCalls.length).toBeGreaterThan(0);
    expect(photoCalls[0][0]).toBe("/api/jobs/job-1/photos");
  });

  it("renders the photo as an image", async () => {
    routeGet();
    const w = await openPhotosTab(await mountView());

    const grid = w.find('[data-testid="job-photos-grid"]');
    expect(grid.exists()).toBe(true);
    const img = w.find('[data-testid="authed-img"]');
    expect(img.exists()).toBe(true);
    expect(img.attributes("src")).toBe("/api/documents/doc-1/download");
  });

  it("says 'no photos' only when the job really has none", async () => {
    routeGet({ photos: [] });
    const w = await openPhotosTab(await mountView());

    expect(w.find('[data-testid="job-photos-empty"]').exists()).toBe(true);
    expect(w.find('[data-testid="job-photos-error"]').exists()).toBe(false);
  });

  it("distinguishes a denied read from an empty job", async () => {
    routeGet({ photosError: Object.assign(new Error("nope"), { status: 404 }) });
    const w = await openPhotosTab(await mountView());

    const err = w.find('[data-testid="job-photos-error"]');
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("access");
    // Crucially NOT the empty state — that was the lie.
    expect(w.find('[data-testid="job-photos-empty"]').exists()).toBe(false);
  });

  it("distinguishes a failed load from an empty job", async () => {
    routeGet({ photosError: Object.assign(new Error("boom"), { status: 500 }) });
    const w = await openPhotosTab(await mountView());

    const err = w.find('[data-testid="job-photos-error"]');
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("Couldn't load");
  });

  it("refetches photos after an upload, not documents", async () => {
    routeGet();
    const w = await mountView();
    getMock.mockClear();

    await w.vm.handlePhotoUpload({ files: [new File(["x"], "a.jpg")] });
    await flushPromises();

    expect(requestMock).toHaveBeenCalledWith(
      "/api/jobs/job-1/photos",
      expect.objectContaining({ method: "POST" }),
    );
    const urls = getMock.mock.calls.map(([u]) => String(u));
    expect(urls).toContain("/api/jobs/job-1/photos");
    expect(urls.some((u) => u.startsWith("/api/documents?job_id="))).toBe(false);
  });
});

/**
 * Sharing a photo with the customer (Doug 2026-08-12: "per photo default off").
 *
 * Job photos are internal until the office says otherwise. This is the control
 * that says otherwise, and the state it shows has to be true — an optimistic
 * checkbox that keeps its new position after a failed PATCH tells the office a
 * customer can see a photo they can't, or that an internal one is withheld
 * when it is still shared.
 */
describe("office job page — sharing a photo with the customer", () => {
  // Built fresh per test on purpose: togglePhotoShare MUTATES the photo it is
  // handed, so a shared module-level fixture carries one test's state into the
  // next and the rollback assertion passes for the wrong reason.
  const shared = () => ({ ...PHOTO, id: "photo-shared", customer_visible: true });
  const internal = () => ({ ...PHOTO, id: "photo-internal", customer_visible: false });

  it("shows each photo's share state in words, not just a checkbox", async () => {
    routeGet({ photos: [shared(), internal()] });
    const w = await openPhotosTab(await mountView());

    const labels = w.findAll('[data-testid^="job-photo-share-"]').map((el) => el.text());
    expect(labels).toContain("Customer can see this");
    expect(labels).toContain("Internal only");
  });

  it("PATCHes the photo when the office shares it", async () => {
    routeGet({ photos: [internal()] });
    const w = await openPhotosTab(await mountView());

    await w.vm.togglePhotoShare(w.vm.jobPhotos[0]);
    await flushPromises();

    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-1/photos/photo-internal",
      { customer_visible: true },
      expect.objectContaining({ successMessage: expect.stringContaining("Shared") }),
    );
  });

  it("rolls the checkbox back when the PATCH fails", async () => {
    routeGet({ photos: [internal()] });
    patchMock.mockRejectedValueOnce(new Error("nope"));
    const w = await openPhotosTab(await mountView());

    await w.vm.togglePhotoShare(w.vm.jobPhotos[0]);
    await flushPromises();

    // Back to internal — never leave the office believing a share happened.
    expect(w.vm.jobPhotos[0].customer_visible).toBe(false);
  });
});
