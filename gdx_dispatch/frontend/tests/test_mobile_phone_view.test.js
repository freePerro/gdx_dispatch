// 2026-08-03 mobile Phone.com companions — behavioral coverage for
// MobilePhoneView: the voicemail tab filters server-side, cards render
// from the API payload, and switching tabs refetches without the filter.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const getMock = vi.fn();
const postMock = vi.fn();

vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock }),
}));

import MobilePhoneView from "../src/views/MobilePhoneView.vue";

const CALLS_PAGE = {
  total: 2,
  items: [
    {
      id: "call-1",
      direction: "in",
      from_number: "+15555550100",
      customer_name: "Test Customer",
      started_at: "2026-08-03T15:00:00Z",
      duration_s: 65,
      status: "voicemail",
      has_voicemail: true,
      has_recording: false,
    },
    {
      id: "call-2",
      direction: "in",
      from_number: "+15555550101",
      customer_name: null,
      started_at: "2026-08-03T14:00:00Z",
      duration_s: 30,
      status: "voicemail",
      has_voicemail: true,
      has_recording: false,
    },
  ],
};

const mountView = () =>
  mount(MobilePhoneView, {
    global: {
      plugins: [createPinia()],
      stubs: { Dialog: true, Button: true, teleport: true },
      directives: { tooltip: {} },
    },
  });

beforeEach(() => {
  setActivePinia(createPinia());
  getMock.mockReset();
  postMock.mockReset();
  getMock.mockResolvedValue(CALLS_PAGE);
});

describe("MobilePhoneView", () => {
  it("defaults to the voicemail tab and filters server-side", async () => {
    mountView();
    await flushPromises();
    expect(getMock).toHaveBeenCalledTimes(1);
    const url = getMock.mock.calls[0][0];
    expect(url).toContain("/api/phone-com/calls?");
    expect(url).toContain("has_voicemail=true");
    expect(url).toContain("page=1");
  });

  it("renders a card per call with caller label and voicemail flag", async () => {
    const wrapper = mountView();
    await flushPromises();
    const rows = wrapper.findAll('[data-test="mp-call-row"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].text()).toContain("Test Customer");
    // No customer match → falls back to the number.
    expect(rows[1].text()).toContain("+15555550101");
    expect(wrapper.findAll('[data-test="mp-vm-flag"]')).toHaveLength(2);
  });

  it("switching to the calls tab refetches WITHOUT the voicemail filter", async () => {
    const wrapper = mountView();
    await flushPromises();
    getMock.mockClear();
    getMock.mockResolvedValue({ total: 0, items: [] });
    await wrapper.find('[data-test="mp-tab-calls"]').trigger("click");
    await flushPromises();
    expect(getMock).toHaveBeenCalledTimes(1);
    expect(getMock.mock.calls[0][0]).not.toContain("has_voicemail");
  });

  it("shows Load more only when more rows exist, and appends the next page", async () => {
    getMock.mockResolvedValue({ total: 60, items: CALLS_PAGE.items });
    const wrapper = mountView();
    await flushPromises();
    const more = wrapper.find('[data-test="mp-load-more"]');
    expect(more.exists()).toBe(true);
    getMock.mockClear();
    getMock.mockResolvedValue({
      total: 60,
      items: [{ ...CALLS_PAGE.items[0], id: "call-3" }],
    });
    await more.trigger("click");
    await flushPromises();
    expect(getMock.mock.calls[0][0]).toContain("page=2");
    expect(wrapper.findAll('[data-test="mp-call-row"]')).toHaveLength(3);
  });
});
