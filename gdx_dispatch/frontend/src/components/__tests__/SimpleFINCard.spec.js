/**
 * SimpleFIN card — connect flow, status/quota render, settings save,
 * Sync Now cap gating, re-link dialog trigger.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ToastService from 'primevue/toastservice';

import SimpleFINCard from '../SimpleFINCard.vue';

// The card reports outcomes through toasts, so "did the user get told?" is only
// answerable by watching them.
const toastSpy = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastSpy }) }));

function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok, status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const DISCONNECTED = {
  connected: false, auth_state: null, stale: false, stale_after_hours: 48,
  quota: { used: 0, cap: 20, remaining: 20, date: '2026-08-13' },
  schedule: {
    frequency: 'manual', fetch_window_start: null, fetch_window_end: null,
    daily_fetch_cap: 20, daily_fetch_cap_max: 20,
  },
  backfill_done: true, accounts: [], timezone: 'America/Chicago',
};

const CONNECTED = {
  ...DISCONNECTED,
  connected: true, auth_state: 'healthy',
  last_synced_at: '2026-08-13T12:00:00+00:00',
  quota: { used: 7, cap: 20, remaining: 13, date: '2026-08-13' },
  schedule: {
    frequency: 'daily', fetch_window_start: '07:00', fetch_window_end: '19:00',
    daily_fetch_cap: 20, daily_fetch_cap_max: 20,
    last_run_at: null, next_run_at: null, last_run_status: null, last_run_error: null,
    backfill_days: 365,
  },
  accounts: [{
    id: 'acct-uuid-1', external_account_id: 'A1', name: 'Business Checking 2204',
    account_number_masked: '•2204', sync_enabled: true, is_inactive: false,
    initial_backfill_done: true, backfill_synced_through: '2026-08-13',
  }],
};

function mountCard() {
  return mount(SimpleFINCard, {
    global: { plugins: [PrimeVue, ToastService, createPinia()] },
  });
}

// PrimeVue teleports Dialog to <body>, so a wrapper.find() cannot see any of
// its contents. Every assertion about what the re-link dialog actually RENDERS
// has to attach to the document and query it there — otherwise the confidence
// badges, the ambiguity warning and the Skip label are shipped untested and the
// spec is only checking component state.
function mountCardAttached() {
  return mount(SimpleFINCard, {
    attachTo: document.body,
    global: { plugins: [PrimeVue, ToastService, createPinia()] },
  });
}

describe('SimpleFINCard', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    toastSpy.mockClear();
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    localStorage.setItem('token', 'test-jwt');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    // PrimeVue teleports Dialog to <body> from attached AND detached mounts,
    // so a leftover dialog would satisfy the next test's document.querySelector.
    document.body.innerHTML = '';
  });

  it('renders the connect flow when not connected', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse(DISCONNECTED));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Not Connected');
    expect(wrapper.find('[data-testid="sfin-token-input"]').exists()).toBe(true);
    const btn = wrapper.find('[data-testid="sfin-connect-btn"]');
    expect(btn.exists()).toBe(true);
    expect(btn.attributes('disabled')).toBeDefined(); // empty token = disabled
  });

  it('posts the setup token and opens the re-link dialog on proposals', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(DISCONNECTED))                       // initial load
      .mockResolvedValueOnce(mkResponse({                                     // connect
        connected: true, bridge_host: 'bridge.example',
        preview: {
          incoming: [{ id: 'NEW-9', name: 'Checking', balance: '1.00' }],
          orphaned: [{ account_id: 'acct-uuid-1', name: 'Checking', external_account_id: 'OLD-1' }],
          new: [{ id: 'NEW-9', name: 'Checking' }],
          proposals: [{ account_id: 'acct-uuid-1', account_name: 'Checking', new_external_id: 'NEW-9', confidence: 1 }],
        },
        warning: null,
      }))
      .mockResolvedValueOnce(mkResponse(CONNECTED));                          // reload
    const wrapper = mountCard();
    await flushPromises();
    await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4tdG9rZW4=');
    await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
    await flushPromises();

    const connectCall = fetchMock.mock.calls[1];
    expect(connectCall[0]).toContain('/api/bank-feeds/simplefin/connect');
    expect(JSON.parse(connectCall[1].body)).toEqual({ setup_token: 'dG9rZW4tdG9rZW4=' });
    // Dialog content is teleported; assert on reactive state instead.
    expect(wrapper.vm.relinkVisible).toBe(true);
    expect(wrapper.vm.relinkProposals).toHaveLength(1);
  });

  it('shows quota, last sync, and saves schedule settings', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse(CONNECTED));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-quota"]').text()).toContain('7/20');
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Connected');

    fetchMock
      .mockResolvedValueOnce(mkResponse({}))          // PUT settings
      .mockResolvedValueOnce(mkResponse(CONNECTED));  // reload
    await wrapper.find('[data-testid="sfin-window-start"]').setValue('06:00');
    await wrapper.find('[data-testid="sfin-window-end"]').setValue('20:00');
    await wrapper.find('[data-testid="sfin-save-settings"]').trigger('click');
    await flushPromises();
    const putCall = fetchMock.mock.calls[1];
    expect(putCall[0]).toContain('/api/bank-feeds/simplefin/settings');
    expect(putCall[1].method).toBe('PUT');
    const body = JSON.parse(putCall[1].body);
    expect(body.fetch_window_start).toBe('06:00');
    expect(body.fetch_window_end).toBe('20:00');
    expect(body.daily_fetch_cap).toBe(20);
  });

  it('disables Sync Now when the daily cap is reached', async () => {
    const capped = {
      ...CONNECTED,
      quota: { used: 20, cap: 20, remaining: 0, date: '2026-08-13' },
    };
    fetchMock.mockResolvedValueOnce(mkResponse(capped));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-sync-now"]').attributes('disabled')).toBeDefined();
    expect(wrapper.text()).toContain('Daily fetch cap reached');
  });

  it('flags a stale connection', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ...CONNECTED, stale: true }));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Stale');
    expect(wrapper.find('[data-testid="sfin-stale-banner"]').exists()).toBe(true);
  });

  it('shows the reconnect banner when the bridge needs re-auth', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ...CONNECTED, auth_state: 'needs_reconnect' }));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Reconnect required');
    expect(wrapper.find('[data-testid="sfin-reconnect-banner"]').exists()).toBe(true);
  });

  // The banner tells the user to "paste a fresh setup token below", and Bank
  // Feeds' "Re-link in Settings" button sends them here to do exactly that.
  // The input used to render only in the !connected branch, so the promised
  // field was absent in the one state the button exists for — the flow
  // dead-ended unless the user first Disconnected, throwing away the history
  // the re-link mapping exists to preserve.
  for (const authState of ['needs_reconnect', 'refresh_failed']) {
    it(`offers the setup-token form while still connected (${authState})`, async () => {
      fetchMock.mockResolvedValueOnce(mkResponse({ ...CONNECTED, auth_state: authState }));
      const wrapper = mountCard();
      await flushPromises();

      expect(wrapper.find('[data-testid="sfin-reconnect-banner"]').exists()).toBe(true);
      expect(wrapper.find('[data-testid="sfin-token-input"]').exists()).toBe(true);
      const btn = wrapper.find('[data-testid="sfin-connect-btn"]');
      expect(btn.exists()).toBe(true);
      expect(btn.text()).toContain('Re-link');
      // Repair wording, not first-time-setup wording: they already have a
      // bridge account, so "Create an account" would read as the wrong screen.
      expect(wrapper.find('[data-testid="sfin-relink-steps"]').exists()).toBe(true);
      expect(wrapper.find('[data-testid="sfin-connect-steps"]').exists()).toBe(false);
      // Still a live connection, so the account/schedule detail stays visible.
      expect(wrapper.find('[data-testid="sfin-quota"]').exists()).toBe(true);
      // And no Disconnect is required to get here.
      expect(wrapper.find('[data-testid="sfin-disconnect"]').exists()).toBe(true);
    });
  }

  // Bank Feeds shows "Re-link in Settings" for `auth_state !== 'healthy'` —
  // an open-world condition. If this card gated its token form on an allowlist
  // of the known bad states, a fifth state added later would render that button
  // and land the user on a card reading a green "Connected" with nothing to do:
  // the dead end this whole change exists to close, quietly restored.
  it('offers the token form for an auth_state it has never seen', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ...CONNECTED, auth_state: 'some_future_state' }));
    const wrapper = mountCard();
    await flushPromises();

    expect(wrapper.find('[data-testid="sfin-token-input"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="sfin-connect-btn"]').exists()).toBe(true);
    // And it must not claim to be fine.
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).not.toBe('Connected');
  });

  it('re-links from the unhealthy state without disconnecting first', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ ...CONNECTED, auth_state: 'needs_reconnect' }))
      .mockResolvedValueOnce(mkResponse({
        connected: true,
        bridge_host: 'bridge.example',
        preview: {
          proposals: [{
            account_id: 'acct-uuid-1', account_name: 'Business Checking 2204',
            new_external_id: 'NEW-9', confidence: 1,
          }],
        },
        warning: null,
      }))
      .mockResolvedValueOnce(mkResponse(CONNECTED));

    const wrapper = mountCard();
    await flushPromises();
    await wrapper.find('[data-testid="sfin-token-input"]').setValue('ZnJlc2gtdG9rZW4=');
    await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
    await flushPromises();

    // Straight to /connect — no /disconnect call anywhere in the flow.
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls[1]).toContain('/api/bank-feeds/simplefin/connect');
    expect(urls.some((u) => u.includes('/simplefin/disconnect'))).toBe(false);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ setup_token: 'ZnJlc2gtdG9rZW4=' });
    // The id-mapping dialog is the point of re-linking: history is preserved.
    expect(wrapper.vm.relinkVisible).toBe(true);
    expect(wrapper.vm.relinkProposals).toHaveLength(1);
  });

  // Applying a mapping is irreversible on bank data: /relink re-points the
  // external id and hard-deletes an absorbed duplicate row and its
  // transactions. Neither sub-exact tier is identity evidence — checked
  // against the real _mask_from_name/_propose_matches, 0.8 pairs
  // "Savings 2204" with "Loan 2204-B Equipment" — so only an exact name match
  // may arrive pre-checked. One click on a wrong row grafts the wrong history.
  it('pre-checks only an exact-match re-link proposal', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(DISCONNECTED))
      .mockResolvedValueOnce(mkResponse({
        connected: true,
        preview: {
          orphaned: [
            { account_id: 'a-exact', name: 'Business Checking', external_account_id: 'O1' },
            { account_id: 'a-mask', name: 'Savings 2204', external_account_id: 'O2' },
            { account_id: 'a-weak', name: 'Payroll Reserve', external_account_id: 'O3' },
          ],
          proposals: [
            // Distinct names on purpose: this test isolates the CONFIDENCE
            // tiers. Duplicate names are their own guard below.
            { account_id: 'a-exact', account_name: 'Business Checking', new_external_id: 'N1', confidence: 1 },
            { account_id: 'a-mask', account_name: 'Savings 2204', new_external_id: 'N2', confidence: 0.8 },
            { account_id: 'a-weak', account_name: 'Payroll Reserve', new_external_id: 'N3', confidence: 0.6 },
          ],
        },
      }))
      .mockResolvedValueOnce(mkResponse(CONNECTED));

    const wrapper = mountCard();
    await flushPromises();
    await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
    await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
    await flushPromises();

    const byId = Object.fromEntries(wrapper.vm.relinkProposals.map((p) => [p.account_id, p.apply]));
    expect(byId['a-exact']).toBe(true);
    expect(byId['a-mask']).toBe(false);
    expect(byId['a-weak']).toBe(false);

    fetchMock
      .mockResolvedValueOnce(mkResponse({ updated: [] }))
      .mockResolvedValueOnce(mkResponse(CONNECTED));
    await wrapper.vm.applyRelink();
    await flushPromises();
    const relinkCall = fetchMock.mock.calls.find((c) => String(c[0]).includes('/simplefin/relink'));
    const sent = JSON.parse(relinkCall[1].body).mappings.map((m) => m.account_id);
    expect(sent).toEqual(['a-exact']);
    // Two rows were left unmapped, so the user is told — see the dedicated
    // partial-mapping test below.
    const warned = toastSpy.mock.calls.map((c) => c[0]).filter((t) => t.severity === 'warn');
    expect(warned).toHaveLength(1);
    expect(warned[0].summary).toMatch(/2 accounts left unmapped/i);
  });

  // Dismissing this dialog without mapping anything is a real outcome: the
  // orphaned accounts keep syncing and report stale, and the dialog only opens
  // from connect(), so getting it back costs another one-time bridge token.
  // It has THREE exits that all land there — Apply with nothing checked, Skip,
  // and the close-X/ESC. Reporting only the first would fix the instance and
  // leave the class, so every exit is asserted.
  describe('dismissing the re-link dialog without mapping', () => {
    async function openDialogWithWeakProposal() {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [{ account_id: 'a-weak', name: 'Checking', external_account_id: 'O3' }],
            proposals: [
              { account_id: 'a-weak', account_name: 'Checking', new_external_id: 'N3', confidence: 0.6 },
            ],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));

      const wrapper = mountCard();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();
      expect(wrapper.vm.relinkVisible).toBe(true);
      expect(wrapper.vm.relinkProposals.every((p) => !p.apply)).toBe(true);
      toastSpy.mockClear();
      return wrapper;
    }

    function dismissalWarnings() {
      return toastSpy.mock.calls.map((c) => c[0]).filter((t) => t.severity === 'warn');
    }

    it('warns when Apply maps nothing', async () => {
      const wrapper = await openDialogWithWeakProposal();
      fetchMock.mockResolvedValueOnce(mkResponse(CONNECTED)); // the reload only
      await wrapper.vm.applyRelink();
      await flushPromises();

      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('/simplefin/relink'))).toBe(false);
      const warned = dismissalWarnings();
      expect(warned).toHaveLength(1);
      expect(warned[0].summary).toMatch(/nothing re-linked/i);
      expect(warned[0].detail).toMatch(/re-issued those accounts under new ids/i);
      // No open-ended "re-link them later" promise: once a sync has run the new
      // ids are ordinary accounts, _propose_matches returns [], and this step
      // is no longer offered. The wording is scoped to that, not absolute.
      expect(warned[0].detail).toMatch(/no longer offered/i);
    });

    it('warns when the dialog is skipped', async () => {
      const wrapper = await openDialogWithWeakProposal();
      wrapper.vm.relinkVisible = false;
      await flushPromises();
      expect(dismissalWarnings()).toHaveLength(1);
    });

    // With only exact matches pre-checked, mapping SOME and leaving others is
    // the normal outcome. Keying the notice off "did anything post?" would let
    // those others pass silently — the same hole one layer in.
    it('warns about the ones left behind even when some were mapped', async () => {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [
              { account_id: 'a-ok', name: 'Business Checking', external_account_id: 'O1' },
              { account_id: 'a-skip1', name: 'Savings 2204', external_account_id: 'O2' },
              { account_id: 'a-skip2', name: 'Payroll', external_account_id: 'O3' },
            ],
            proposals: [
              { account_id: 'a-ok', account_name: 'Business Checking', new_external_id: 'N1', confidence: 1 },
              { account_id: 'a-skip1', account_name: 'Savings 2204', new_external_id: 'N2', confidence: 0.8 },
              { account_id: 'a-skip2', account_name: 'Payroll', new_external_id: 'N3', confidence: 0.6 },
            ],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCard();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();
      toastSpy.mockClear();

      fetchMock
        .mockResolvedValueOnce(mkResponse({ updated: ['a-ok'] }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      await wrapper.vm.applyRelink();
      await flushPromises();

      const kinds = toastSpy.mock.calls.map((c) => c[0]);
      expect(kinds.filter((t) => t.severity === 'success')).toHaveLength(1);
      const warned = kinds.filter((t) => t.severity === 'warn');
      expect(warned).toHaveLength(1);
      expect(warned[0].summary).toMatch(/2 accounts left unmapped/i);
      wrapper.unmount();
    });

    it('stays quiet when every proposal was mapped', async () => {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [{ account_id: 'a-ok', name: 'Business Checking', external_account_id: 'O1' }],
            proposals: [
              { account_id: 'a-ok', account_name: 'Business Checking', new_external_id: 'N1', confidence: 1 },
            ],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCard();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();
      toastSpy.mockClear();

      fetchMock
        .mockResolvedValueOnce(mkResponse({ updated: ['a-ok'] }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      await wrapper.vm.applyRelink();
      await flushPromises();
      expect(toastSpy.mock.calls.map((c) => c[0]).filter((t) => t.severity === 'warn')).toHaveLength(0);
      wrapper.unmount();
    });

    // The likely production shape, and the one with no dialog at all: a bridge
    // re-auth that also reformats account names matches nothing, so the server
    // returns orphans with ZERO proposals. Counting "proposals left unchecked"
    // reports nothing here — the user would get a green "connected" toast while
    // half the feed quietly stopped receiving data. The at-risk set is the
    // orphans, so that is what the notice counts.
    it('warns about re-issued accounts even when nothing could be proposed', async () => {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [
              { account_id: 'o-1', name: 'Checking ****1234', external_account_id: 'OLD-1' },
              { account_id: 'o-2', name: 'Savings ****5678', external_account_id: 'OLD-2' },
            ],
            proposals: [],
            new: [{ id: 'NEW-A', name: 'CHECKING-1234' }],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCard();
      await flushPromises();
      toastSpy.mockClear();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();

      // No dialog to dismiss — the notice has to come from the connect path.
      expect(wrapper.vm.relinkVisible).toBe(false);
      const warned = toastSpy.mock.calls.map((c) => c[0]).filter((t) => t.severity === 'warn');
      expect(warned).toHaveLength(1);
      expect(warned[0].summary).toMatch(/nothing re-linked/i);
      wrapper.unmount();
    });

    // An orphan the server proposed nothing for is invisible in the dialog, so
    // it must still be counted when the dialog closes.
    it('counts orphans with no proposal alongside the unchecked ones', async () => {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [
              { account_id: 'a-ok', name: 'Business Checking', external_account_id: 'O1' },
              { account_id: 'o-unproposed-1', name: 'Money Market', external_account_id: 'O2' },
              { account_id: 'o-unproposed-2', name: 'Reserve', external_account_id: 'O3' },
            ],
            proposals: [
              { account_id: 'a-ok', account_name: 'Business Checking', new_external_id: 'N1', confidence: 1 },
            ],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCard();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();
      toastSpy.mockClear();

      fetchMock
        .mockResolvedValueOnce(mkResponse({ updated: ['a-ok'] }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      await wrapper.vm.applyRelink();
      await flushPromises();

      const warned = toastSpy.mock.calls.map((c) => c[0]).filter((t) => t.severity === 'warn');
      expect(warned).toHaveLength(1);
      // Two orphans had no proposal at all; neither appeared in the dialog.
      expect(warned[0].summary).toMatch(/2 accounts left unmapped/i);
      wrapper.unmount();
    });

    it('warns when the dialog is closed by the X or ESC', async () => {
      const wrapper = await openDialogWithWeakProposal();
      // Both the header close button and ESC route through PrimeVue's own
      // close path, which writes the same v-model the watcher observes.
      wrapper.findComponent({ name: 'Dialog' }).vm.close();
      await flushPromises();
      expect(dismissalWarnings()).toHaveLength(1);
    });
  });

  // These render the dialog for real rather than reading wrapper.vm, because
  // the checkbox defaults and the badges are the whole safety story here.
  describe('the re-link dialog as rendered', () => {
    const PROPOSALS = [
      { account_id: 'a-exact', account_name: 'Business Checking', new_external_id: 'N1', confidence: 1 },
      { account_id: 'a-mask', account_name: 'Savings 2204', new_external_id: 'N2', confidence: 0.8 },
      { account_id: 'a-dupA', account_name: 'Checking', new_external_id: 'N3', confidence: 1 },
      { account_id: 'a-dupB', account_name: 'Checking', new_external_id: 'N4', confidence: 1 },
    ];
    // Ambiguity is derived from the FULL orphan list, not the matched subset,
    // so the fixture has to carry it the way /connect does.
    const ORPHANED = PROPOSALS.map((p) => ({
      account_id: p.account_id, name: p.account_name, external_account_id: `OLD-${p.account_id}`,
    }));

    async function openRendered() {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: { proposals: PROPOSALS, orphaned: ORPHANED },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCardAttached();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();
      return wrapper;
    }

    afterEach(() => { document.body.innerHTML = ''; });

    it('renders a confidence badge per row, flagging the weak ones', async () => {
      const wrapper = await openRendered();
      const badge = (id) => document.querySelector(`[data-testid="sfin-relink-confidence-${id}"]`);

      expect(badge('a-exact').textContent.trim()).toBe('exact');
      expect(badge('a-exact').className).not.toContain('weak');

      expect(badge('a-mask').textContent.trim()).toBe('80% match');
      expect(badge('a-mask').className).toContain('weak');

      // Two accounts share the name "Checking", so neither 1.0 is evidence.
      expect(badge('a-dupA').textContent).toMatch(/more than one account/i);
      expect(badge('a-dupB').className).toContain('weak');
      wrapper.unmount();
    });

    it('checks only the unambiguous exact match', async () => {
      const wrapper = await openRendered();
      const checked = (id) => document.querySelector(`#rl-${id}`).checked;
      expect(checked('a-exact')).toBe(true);
      expect(checked('a-mask')).toBe(false);
      expect(checked('a-dupA')).toBe(false);
      expect(checked('a-dupB')).toBe(false);
      wrapper.unmount();
    });

    // One STORED account named "Checking" against TWO incoming "Checking"
    // accounts yields a single 1.0 proposal, paired to whichever the bridge
    // listed first. The orphan side looks unique; the coin flip is on the
    // candidate side, which only preview.new exposes. Confirmed by running the
    // real _propose_matches.
    it('treats a name duplicated among the bridge candidates as ambiguous', async () => {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [{ account_id: 'a-one', name: 'Checking', external_account_id: 'OLD-1' }],
            proposals: [
              { account_id: 'a-one', account_name: 'Checking', new_external_id: 'NEW-A', confidence: 1 },
            ],
            new: [
              { id: 'NEW-A', name: 'Checking' },
              { id: 'NEW-B', name: 'Checking' },
            ],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCardAttached();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();

      expect(wrapper.vm.relinkProposals[0].apply).toBe(false);
      expect(document.querySelector('#rl-a-one').checked).toBe(false);
      expect(
        document.querySelector('[data-testid="sfin-relink-confidence-a-one"]').textContent,
      ).toMatch(/more than one account/i);
      wrapper.unmount();
    });

    // TWO stored accounts named "Checking", ONE incoming "Checking". The
    // server's `used` set lets only one proposal out, so counting the orphan
    // side from `proposals` would see a single "Checking" and call it unique —
    // while the server in fact picked one of two identically-named rows
    // arbitrarily. Counting from `preview.orphaned` is what catches it.
    it('treats a name duplicated among the stored accounts as ambiguous', async () => {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [
              { account_id: 'o-1', name: 'Checking', external_account_id: 'OLD-1' },
              { account_id: 'o-2', name: 'Checking', external_account_id: 'OLD-2' },
            ],
            proposals: [
              { account_id: 'o-1', account_name: 'Checking', new_external_id: 'NEW-A', confidence: 1 },
            ],
            new: [{ id: 'NEW-A', name: 'Checking' }],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCardAttached();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();

      expect(wrapper.vm.relinkProposals[0].apply).toBe(false);
      expect(document.querySelector('#rl-o-1').checked).toBe(false);
      wrapper.unmount();
    });

    // The right-hand side is an opaque bridge id; the user is asked to
    // recognise the pairing, so the candidate's own name has to be on screen.
    it('names the candidate account rather than only its bridge id', async () => {
      fetchMock
        .mockResolvedValueOnce(mkResponse(DISCONNECTED))
        .mockResolvedValueOnce(mkResponse({
          connected: true,
          preview: {
            orphaned: [{ account_id: 'a-ok', name: 'Business Checking', external_account_id: 'OLD-1' }],
            proposals: [
              { account_id: 'a-ok', account_name: 'Business Checking', new_external_id: 'NEW-7', confidence: 1 },
            ],
            new: [{ id: 'NEW-7', name: 'Business Checking 2204', balance: '1543.22' }],
          },
        }))
        .mockResolvedValueOnce(mkResponse(CONNECTED));
      const wrapper = mountCardAttached();
      await flushPromises();
      await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4=');
      await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
      await flushPromises();

      const label = document.querySelector('label[for="rl-a-ok"]').textContent;
      expect(label).toContain('Business Checking 2204');
      expect(label).toContain('1543.22');
      wrapper.unmount();
    });

    it('warns when the rendered Skip button is clicked', async () => {
      const wrapper = await openRendered();
      toastSpy.mockClear();
      const skip = document.querySelector('[data-testid="sfin-relink-skip"]');
      expect(skip).not.toBeNull();
      expect(skip.textContent).toMatch(/add them as new/i);
      skip.click();
      await flushPromises();
      const warned = toastSpy.mock.calls.map((c) => c[0]).filter((t) => t.severity === 'warn');
      expect(warned).toHaveLength(1);
      expect(warned[0].detail).toMatch(/re-issued those accounts under new ids/i);
      wrapper.unmount();
    });
  });

  it('requires a second click to disconnect', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse(CONNECTED));
    const wrapper = mountCard();
    await flushPromises();
    const btn = wrapper.find('[data-testid="sfin-disconnect"]');
    await btn.trigger('click');
    expect(fetchMock).toHaveBeenCalledTimes(1); // still only the initial load
    expect(btn.text()).toContain('Really disconnect?');
    fetchMock
      .mockResolvedValueOnce(mkResponse({ disconnected: 1 }))
      .mockResolvedValueOnce(mkResponse(DISCONNECTED));
    await btn.trigger('click');
    await flushPromises();
    expect(fetchMock.mock.calls[1][0]).toContain('/api/bank-feeds/simplefin/disconnect');
  });
});
