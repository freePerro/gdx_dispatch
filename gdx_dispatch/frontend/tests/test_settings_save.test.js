import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SettingsView from "../src/views/SettingsView.vue";

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

const postMock = vi.fn();
const patchMock = vi.fn();
const getMock = vi.fn();
const requestMock = vi.fn();
const applyThemeVarsMock = vi.fn();

vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock, patch: patchMock, request: requestMock }),
}));

vi.mock("../src/stores/theme", () => ({
  useThemeStore: () => ({
    branding: {
      company_name: "Tenant Co",
      primary_color: "#0057a8",
      accent_color: "#f7b500",
    },
    applyThemeVars: applyThemeVarsMock,
  }),
}));

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  // New PrimeVue v4 Tabs API (replaces deprecated TabView/TabPanel pair)
  Tabs: { template: "<div><slot /></div>" },
  TabList: { template: "<div><slot /></div>" },
  Tab: { props: ["value"], template: "<button>{{ value }}<slot /></button>" },
  TabPanels: { template: "<div><slot /></div>" },
  TabPanel: {
    props: ["value", "header"],
    template: "<section><slot /></section>",
  },
  TabView: { template: "<div><slot /></div>" },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Button: {
    props: ["label"],
    emits: ["click"],
    template: '<button @click="$emit(\'click\')">{{ label }}</button>',
  },
  Badge: { props: ["value"], template: "<span>{{ value }}</span>" },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Column: { template: "<div />" },
  DataTable: { template: "<div><slot /></div>" },
  Dialog: { props: ["visible"], template: "<div v-if='visible'><slot /></div>" },
  Divider: { template: "<hr />" },
  Select: { props: ["modelValue", "options"], template: "<select></select>" },
  Password: { props: ["modelValue"], template: "<input type='password' />" },
  ToggleSwitch: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<button @click="$emit(\'update:modelValue\', !modelValue)" :data-checked="modelValue">tgl</button>',
  },
  ProgressSpinner: { template: "<div />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  InputText: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  ColorPicker: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<input type="color" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  AIAssistantIntegrationCard: { template: "<div data-stub='ai-card' />" },
  PhoneComIntegrationCard: { template: "<div data-stub='pc-card' />" },
  OutlookIntegrationCard: { template: "<div data-stub='outlook-card' />" },
  OutlookConnectButton: { template: "<div data-stub='outlook-btn' />" },
};

describe("Settings branding save", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    requestMock.mockReset();
    applyThemeVarsMock.mockReset();
    getMock.mockResolvedValue({
      tenant_tier: "starter",
      modules: [{ key: "jobs", name: "Jobs", tier: "starter", enabled: true, locked: false }],
    });
    postMock.mockResolvedValue({});
    patchMock.mockResolvedValue({});
    requestMock.mockResolvedValue({});
    document.documentElement.style.removeProperty("--primary");
    document.documentElement.style.removeProperty("--accent");
  });

  it("D101 — module toggle stages a pending change; Save triggers the API call", async () => {
    // Pre-fix the toggle fired an immediate POST, so a misclick was instant
    // and irreversible. Now toggling stages a pending change; only Save
    // commits. Revert restores server state.
    // The test flips this AFTER the user clicks Save — at which point the
    // SettingsView reloads modules and we want the persisted-disabled state
    // back. Other GETs (branding, ai-settings, ai-audit, google-maps) are
    // mocked to empty/no-op since they don't drive this flow.
    let warrantiesEnabled = true;
    getMock.mockImplementation((url) => {
      if (url === "/api/settings/modules") {
        return Promise.resolve({
          tenant_tier: "professional",
          modules: [
            { key: "warranties", name: "Warranties", tier: "professional", enabled: warrantiesEnabled, locked: false },
          ],
        });
      }
      return Promise.resolve({});
    });
    const wrapper = mount(SettingsView, { global: { stubs } });
    await flushPromises();

    const toggle = wrapper.find("[data-testid='module-toggle-warranties']");
    expect(toggle.exists()).toBe(true);
    await toggle.trigger("click");

    // Pending — no API call yet, dirty hint visible.
    expect(postMock).not.toHaveBeenCalled();
    expect(wrapper.find("[data-testid='module-pending-flag']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='modules-dirty-hint']").exists()).toBe(true);

    // Flip the persisted state so the post-save reload shows the new
    // disabled value. The reload happens inside SettingsView's save flow
    // after the POST returns.
    warrantiesEnabled = false;
    await wrapper.find("[data-testid='modules-save-btn']").trigger("click");
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith("/api/settings/modules/warranties/disable", {});
    expect(wrapper.find("[data-testid='module-pending-flag']").exists()).toBe(false);
  });

  it("D101 — Revert clears pending changes without firing the API", async () => {
    getMock.mockImplementation((url) => {
      if (url === "/api/settings/branding") return Promise.resolve({});
      if (url === "/api/settings/modules") {
        return Promise.resolve({
          tenant_tier: "professional",
          modules: [
            { key: "warranties", name: "Warranties", tier: "professional", enabled: true, locked: false },
          ],
        });
      }
      return Promise.resolve({});
    });
    const wrapper = mount(SettingsView, { global: { stubs } });
    await flushPromises();

    await wrapper.find("[data-testid='module-toggle-warranties']").trigger("click");
    expect(wrapper.find("[data-testid='module-pending-flag']").exists()).toBe(true);

    await wrapper.find("[data-testid='modules-revert-btn']").trigger("click");
    expect(postMock).not.toHaveBeenCalled();
    expect(wrapper.find("[data-testid='module-pending-flag']").exists()).toBe(false);
  });

  it("submits branding form and applies CSS theme vars", async () => {
    const wrapper = mount(SettingsView, { global: { stubs } });

    await wrapper.find("[data-testid='company-name']").setValue("GDX Dispatch");
    await wrapper.find("[data-testid='primary-color']").setValue("#123456");
    await wrapper.find("[data-testid='secondary-color']").setValue("#abcdef");
    await wrapper.find("[data-testid='save-branding']").trigger("click");

    expect(patchMock).toHaveBeenCalledWith("/api/settings/branding", {
      company_name: "GDX Dispatch",
      primary_color: "#123456",
      secondary_color: "#abcdef",
    });
    expect(document.documentElement.style.getPropertyValue("--primary")).toBe("#123456");
    expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#abcdef");
    expect(applyThemeVarsMock).toHaveBeenCalled();
  });
});
