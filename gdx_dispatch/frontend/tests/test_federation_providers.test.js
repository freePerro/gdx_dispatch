import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import FederationProviders from "../src/views/FederationProviders.vue";

function mockFetch(responses) {
  const queue = [...responses];
  globalThis.fetch = vi.fn(() => {
    const next = queue.shift() || { ok: true, status: 200, body: {} };
    return Promise.resolve({
      ok: next.ok !== false,
      status: next.status || 200,
      json: async () => next.body,
    });
  });
}

// AppLayout pulls in AppSidebar (Pinia store) — stub.
const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
};
const mountOpts = { global: { stubs } };

describe("FederationProviders.vue", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    globalThis.fetch = undefined;
  });

  it("renders provider rows from GET /api/federation/providers", async () => {
    mockFetch([
      {
        body: {
          total: 1,
          items: [
            {
              id: "aabbccdd",
              tenant_id: "t1",
              kind: "oidc",
              display_name: "Okta",
              metadata_url: "https://example.com/.well-known/openid-configuration",
              client_id: "cid",
              has_client_secret: true,
              redirect_uri: "https://gdx/callback",
              sp_entity_id: null,
              acs_url: null,
              created_at: 1713600000,
            },
          ],
        },
      },
    ]);
    const wrapper = mount(FederationProviders, mountOpts);
    await flushPromises();
    expect(wrapper.find('[data-testid="federation-providers-view"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="fed-row-aabbccdd"]').exists()).toBe(true);
  });

  it("renders error state on fetch failure", async () => {
    mockFetch([{ ok: false, status: 500, body: { detail: "boom" } }]);
    const wrapper = mount(FederationProviders, mountOpts);
    await flushPromises();
    expect(wrapper.find('[data-testid="error-message"]').exists()).toBe(true);
  });
});
