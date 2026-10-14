/**
 * Event Rules page — who sees the write controls (GDXA-1097).
 *
 * The server 403s POST/PUT/DELETE /api/workflows for every role outside
 * dispatch-manager (owner, admin, dispatcher, manager). The page shows those
 * roles New rule, edit and delete, and shows everyone else the list and the
 * run history with no control that would 403.
 *
 * Real DataTable/Column (tests/setup.js installs PrimeVue); Dialog stubbed.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: vi.fn(), put: vi.fn(), delete: vi.fn() }),
}));
let role = "dispatcher";
vi.mock("../../stores/auth", () => ({ useAuthStore: () => ({ user: { role } }) }));

const RULES = [
  { id: "r1", name: "Thank-you", trigger_event: "invoice.paid", run_count: 2,
    actions: [{ action_type: "send_email", params: { subject: "Thanks", body: "Hi" } }] },
];

function routeGet(url) {
  if (url === "/api/workflows") return Promise.resolve(RULES.map((r) => ({ ...r })));
  if (url === "/api/settings") return Promise.resolve({ automation_emails_enabled: true });
  return Promise.resolve([]);
}

async function mountView() {
  const { default: View } = await import("../AutomationRulesView.vue");
  const w = mount(View, {
    global: {
      stubs: {
        RouterLink: { template: "<a><slot /></a>" },
        Toast: true,
        Dialog: {
          props: ["visible", "header"],
          template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
        },
      },
    },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  role = "dispatcher";
  getMock.mockReset().mockImplementation(routeGet);
});

describe("AutomationRulesView write controls", () => {
  it.each(["owner", "admin", "dispatcher", "manager"])("%s sees New rule, edit and delete", async (r) => {
    role = r;
    const w = await mountView();
    expect(w.find('[data-testid="ar-new"]').exists()).toBe(true);
    expect(w.find('[data-testid="ar-edit"]').exists()).toBe(true);
    expect(w.find('[data-testid="ar-delete"]').exists()).toBe(true);
    await w.find('[data-testid="ar-new"]').trigger("click");
    expect(w.find('[data-testid="ar-save"]').exists()).toBe(true);
  });

  it.each(["technician", "sales", "accounting", "viewer"])(
    "%s reads the rules and their runs with no write control",
    async (r) => {
      role = r;
      const w = await mountView();
      expect(w.text()).toContain("Thank-you");
      expect(w.find('[data-testid="ar-runs-link"]').exists()).toBe(true);
      expect(w.find('[data-testid="ar-new"]').exists()).toBe(false);
      expect(w.find('[data-testid="ar-edit"]').exists()).toBe(false);
      expect(w.find('[data-testid="ar-delete"]').exists()).toBe(false);
      expect(w.find('[data-testid="ar-save"]').exists()).toBe(false);
    },
  );
});
