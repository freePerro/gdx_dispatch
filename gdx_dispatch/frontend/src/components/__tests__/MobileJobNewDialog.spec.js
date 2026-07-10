/**
 * MobileJobNewDialog — pin the create flow.
 *
 * 2026-05-10 (Doug): "tech need to be able to make a new job, and add a
 * new customer and parts while doing it."
 *
 * Pinned contract:
 *  - Title is required; submit disabled when empty.
 *  - "Create new customer" toggle reveals 4 fields; name is required.
 *  - Submit POSTs in order: /api/customers (if new) → /api/jobs → per-part
 *    /api/jobs/{id}/parts-needed.
 *  - Customer search debounces and hits /api/customers/search.
 *  - Part SKU autocomplete hits /api/parts-needed/sku-suggest.
 *  - Emits "created" with the new job and resets state on success.
 *  - Dirty form → Cancel asks for confirmation; clean form doesn't.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const toastAdd = vi.fn();
const hasPermission = vi.fn(() => true);

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission,
    permissions: { value: ['jobs.write', 'inventory.write'] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));

import MobileJobNewDialog from '../MobileJobNewDialog.vue';

// Lightweight stubs — Dialog renders its slot inline so we can interact
// with the form. Toggle/Button/Input stubs preserve v-model + click.
const stubs = {
  Dialog: {
    props: ['visible'],
    emits: ['update:visible'],
    template: '<div data-testid="dlg" v-if="visible"><slot /><div class="footer"><slot name="footer" /></div></div>',
  },
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'loading', 'disabled', 'size'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'input'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value); $emit(\'input\', $event)" />',
    inheritAttrs: false,
  },
  // PhoneInput wraps PrimeVue InputMask; stub it like InputText (emit raw
  // value) so setInput drives it without the PrimeVue plugin.
  PhoneInput: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'input'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value); $emit(\'input\', $event)" />',
    inheritAttrs: false,
  },
  FormField: {
    props: ['modelValue', 'label', 'required', 'type', 'as', 'rows', 'placeholder', 'autocomplete'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :data-testid="$attrs[\'data-testid\']" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
    inheritAttrs: false,
  },
};

function mountDialog(props = { visible: true }) {
  return mount(MobileJobNewDialog, { props, global: { stubs } });
}

async function setInput(wrapper, testid, value) {
  const el = wrapper.find(`[data-testid="${testid}"]`);
  el.element.value = value;
  await el.trigger('input');
}

describe('MobileJobNewDialog', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    toastAdd.mockReset();
    hasPermission.mockReset();
    hasPermission.mockReturnValue(true);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('disables submit until title is entered', async () => {
    const wrapper = mountDialog();
    await flushPromises();

    const submit = wrapper.find('[data-testid="mjn-submit"]');
    expect(submit.attributes('disabled')).toBeDefined();

    await setInput(wrapper, 'mjn-job-title', 'Replace springs');
    expect(wrapper.find('[data-testid="mjn-submit"]').attributes('disabled')).toBeUndefined();
  });

  it('requires customer name when new-customer toggle is on', async () => {
    const wrapper = mountDialog();
    await flushPromises();

    await setInput(wrapper, 'mjn-job-title', 'Replace springs');
    expect(wrapper.find('[data-testid="mjn-submit"]').attributes('disabled')).toBeUndefined();

    // Flip the new-customer toggle on — submit becomes disabled until name set.
    const toggle = wrapper.find('[data-testid="mjn-new-customer-toggle"]');
    toggle.element.checked = true;
    await toggle.trigger('change');

    expect(wrapper.find('[data-testid="mjn-submit"]').attributes('disabled')).toBeDefined();

    await setInput(wrapper, 'mjn-newcust-name', 'Acme Co');
    expect(wrapper.find('[data-testid="mjn-submit"]').attributes('disabled')).toBeUndefined();
  });

  it('submits in order: customer (if new) → job → parts', async () => {
    apiPost.mockImplementation((url) => {
      if (url === '/api/customers') return Promise.resolve({ id: 'cust-1' });
      if (url === '/api/jobs') return Promise.resolve({ id: 'job-1', title: 'Replace springs' });
      if (url.endsWith('/parts-needed')) return Promise.resolve({ id: 'pn-1' });
      return Promise.resolve({});
    });

    const wrapper = mountDialog();
    await flushPromises();

    await setInput(wrapper, 'mjn-job-title', 'Replace springs');

    // New customer
    const toggle = wrapper.find('[data-testid="mjn-new-customer-toggle"]');
    toggle.element.checked = true;
    await toggle.trigger('change');
    await setInput(wrapper, 'mjn-newcust-name', 'Acme Co');
    await setInput(wrapper, 'mjn-newcust-phone', '555-1234');

    // Add a part
    await wrapper.find('[data-testid="mjn-add-part"]').trigger('click');
    await setInput(wrapper, 'mjn-part-name-0', 'Torsion spring');

    await wrapper.find('[data-testid="mjn-submit"]').trigger('click');
    await flushPromises();

    const calls = apiPost.mock.calls.map((c) => c[0]);
    expect(calls).toEqual([
      '/api/customers',
      '/api/jobs',
      '/api/jobs/job-1/parts-needed',
    ]);

    // Customer payload shape
    expect(apiPost).toHaveBeenNthCalledWith(1, '/api/customers', expect.objectContaining({
      name: 'Acme Co',
      phone: '555-1234',
    }));

    // Job payload shape — customer_id is the just-created customer
    expect(apiPost).toHaveBeenNthCalledWith(2, '/api/jobs', expect.objectContaining({
      title: 'Replace springs',
      customer_id: 'cust-1',
    }));

    // Part payload shape
    expect(apiPost).toHaveBeenNthCalledWith(3, '/api/jobs/job-1/parts-needed', expect.objectContaining({
      part_name: 'Torsion spring',
      quantity: 1,
    }));

    // Emits "created" with the job
    const createdEvents = wrapper.emitted('created') || [];
    expect(createdEvents.length).toBe(1);
    expect(createdEvents[0][0]).toMatchObject({ id: 'job-1' });
  });

  it('skips customer-create when no toggle, uses selected customer id', async () => {
    apiGet.mockResolvedValue([{ id: 'cust-existing', name: 'Existing Co', phone: '555-9999' }]);
    apiPost.mockImplementation((url) => {
      if (url === '/api/jobs') return Promise.resolve({ id: 'job-2' });
      return Promise.resolve({});
    });

    const wrapper = mountDialog();
    await flushPromises();

    // Type into the customer search box, advance debounce timer
    await setInput(wrapper, 'mjn-customer-search', 'Existing');
    vi.advanceTimersByTime(300);
    await flushPromises();

    // Customer search hit /api/customers/search
    expect(apiGet).toHaveBeenCalledWith(expect.stringMatching(/^\/api\/customers\/search\?q=Existing/));

    // Pick the suggestion
    const opt = wrapper.find('[data-testid="mjn-customer-option"]');
    expect(opt.exists()).toBe(true);
    await opt.trigger('click');

    await setInput(wrapper, 'mjn-job-title', 'Adjust opener');

    await wrapper.find('[data-testid="mjn-submit"]').trigger('click');
    await flushPromises();

    // No POST to /api/customers — only /api/jobs.
    const postUrls = apiPost.mock.calls.map((c) => c[0]);
    expect(postUrls).toEqual(['/api/jobs']);
    expect(apiPost).toHaveBeenCalledWith('/api/jobs', expect.objectContaining({
      title: 'Adjust opener',
      customer_id: 'cust-existing',
    }));
  });

  it('keeps the job when a part-add fails (best-effort parts)', async () => {
    apiPost.mockImplementation((url) => {
      if (url === '/api/jobs') return Promise.resolve({ id: 'job-3' });
      if (url.endsWith('/parts-needed')) return Promise.reject(new Error('boom'));
      return Promise.resolve({});
    });

    const wrapper = mountDialog();
    await flushPromises();
    await setInput(wrapper, 'mjn-job-title', 'X');
    await wrapper.find('[data-testid="mjn-add-part"]').trigger('click');
    await setInput(wrapper, 'mjn-part-name-0', 'Mystery part');

    await wrapper.find('[data-testid="mjn-submit"]').trigger('click');
    await flushPromises();

    // The job was created.
    const createdEvents = wrapper.emitted('created') || [];
    expect(createdEvents.length).toBe(1);

    // A warn toast surfaced about the failed part.
    const warnToast = toastAdd.mock.calls.find((c) => c[0]?.severity === 'warn');
    expect(warnToast).toBeTruthy();
    expect(warnToast[0].summary).toMatch(/part.*failed/i);
  });

  it('rolls forward only — customer persists if job-create fails', async () => {
    apiPost.mockImplementation((url) => {
      if (url === '/api/customers') return Promise.resolve({ id: 'cust-9' });
      if (url === '/api/jobs') return Promise.reject(new Error('500'));
      return Promise.resolve({});
    });

    const wrapper = mountDialog();
    await flushPromises();

    const toggle = wrapper.find('[data-testid="mjn-new-customer-toggle"]');
    toggle.element.checked = true;
    await toggle.trigger('change');
    await setInput(wrapper, 'mjn-newcust-name', 'New Customer');
    await setInput(wrapper, 'mjn-job-title', 'Some job');

    await wrapper.find('[data-testid="mjn-submit"]').trigger('click');
    await flushPromises();

    // Customer was POSTed; job was attempted but failed → no created event.
    expect(apiPost).toHaveBeenCalledWith('/api/customers', expect.any(Object));
    expect(apiPost).toHaveBeenCalledWith('/api/jobs', expect.any(Object));
    expect(wrapper.emitted('created')).toBeFalsy();

    // Toast surfaced the job-create error (not the customer-create error).
    const errorToasts = toastAdd.mock.calls.filter((c) => c[0]?.severity === 'error');
    expect(errorToasts.length).toBe(1);
    expect(errorToasts[0][0].summary).toMatch(/job/i);
  });

  it('hides the Parts section when user lacks inventory.write', async () => {
    hasPermission.mockImplementation((k) => k !== 'inventory.write');

    const wrapper = mountDialog();
    await flushPromises();

    expect(wrapper.find('[data-testid="mjn-add-part"]').exists()).toBe(false);
  });

  it('dirty form: Cancel asks before discarding; clean form closes silently', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      const wrapper = mountDialog();
      await flushPromises();

      // Clean → closes without prompting.
      await wrapper.find('[data-testid="mjn-cancel"]').trigger('click');
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(wrapper.emitted('update:visible').at(-1)).toEqual([false]);

      // Re-open, dirty it, decline the confirm → stays open.
      await wrapper.setProps({ visible: true });
      await setInput(wrapper, 'mjn-job-title', 'Replace springs');
      await wrapper.find('[data-testid="mjn-cancel"]').trigger('click');
      expect(confirmSpy).toHaveBeenCalledTimes(1);
      // Last visibility event is still the earlier close; no new close emitted.
      const events = wrapper.emitted('update:visible');
      expect(events.filter((e) => e[0] === false)).toHaveLength(1);
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('SKU autocomplete pulls suggestions from /api/parts-needed/sku-suggest', async () => {
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/parts-needed/sku-suggest')) {
        return Promise.resolve([
          { source: 'parts', sku: 'SPR-200', name: 'Torsion 200', qty_on_hand: 4 },
        ]);
      }
      return Promise.resolve([]);
    });

    const wrapper = mountDialog();
    await flushPromises();
    await wrapper.find('[data-testid="mjn-add-part"]').trigger('click');

    await setInput(wrapper, 'mjn-part-name-0', 'spring');
    vi.advanceTimersByTime(300);
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith(expect.stringMatching(/^\/api\/parts-needed\/sku-suggest\?q=spring/));

    const sug = wrapper.find('[data-testid="mjn-part-suggestion-0"]');
    expect(sug.exists()).toBe(true);
    expect(sug.text()).toContain('SPR-200');
  });
});
