/**
 * MobileChangeOrderDialog — 2026-07-01 UX audit (field change orders).
 *
 * Pins the contract:
 *  1. Submit disabled until a title is entered.
 *  2. Submit posts /api/change-orders via the OFFLINE QUEUE (postQueued)
 *     with status pending_approval and the job/customer context.
 *  3. A queued (offline) result shows the "Saved offline" warn toast and
 *     still closes the dialog; an online result shows the success toast
 *     with the returned co_number.
 *  4. Dirty form → Cancel asks for confirmation; clean form doesn't.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiPostQueued = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ postQueued: apiPostQueued }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));

import MobileChangeOrderDialog from '../MobileChangeOrderDialog.vue';

const stubs = {
  Dialog: {
    props: ['visible'],
    emits: ['update:visible'],
    template:
      '<div data-testid="dlg" v-if="visible"><slot /><div class="footer"><slot name="footer" /></div></div>',
  },
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'loading', 'disabled', 'size'],
    emits: ['click'],
    template:
      '<button :data-label="label" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<input type="number" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)"></textarea>',
  },
};

function mountDialog(props = {}) {
  return mount(MobileChangeOrderDialog, {
    props: {
      visible: true,
      jobId: 'job-1',
      jobTitle: 'Spring replacement',
      customerId: 'cust-9',
      customerName: 'Acme Garage',
      ...props,
    },
    global: { stubs },
  });
}

beforeEach(() => {
  apiPostQueued.mockReset();
  toastAdd.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('MobileChangeOrderDialog', () => {
  it('submit is disabled until a title is typed', async () => {
    const w = mountDialog();
    const submit = w.get('[data-testid="mco-submit"]');
    expect(submit.attributes('disabled')).toBeDefined();
    await w.get('[data-testid="mco-title"]').setValue('Extra hinge');
    expect(submit.attributes('disabled')).toBeUndefined();
  });

  it('posts the CO through the offline queue with pending_approval + context', async () => {
    apiPostQueued.mockResolvedValueOnce({ id: 'co-1', co_number: 'CO-0042' });
    const w = mountDialog();
    await w.get('[data-testid="mco-title"]').setValue('Extra hinge');
    await w.get('[data-testid="mco-amount"]').setValue(80);
    await w.get('[data-testid="mco-submit"]').trigger('click');
    await flushPromises();

    expect(apiPostQueued).toHaveBeenCalledTimes(1);
    const [url, payload, opts] = apiPostQueued.mock.calls[0];
    expect(url).toBe('/api/change-orders');
    expect(payload).toMatchObject({
      job_id: 'job-1',
      customer_id: 'cust-9',
      customer_name: 'Acme Garage',
      title: 'Extra hinge',
      status: 'pending_approval',
      amount: 80,
    });
    expect(opts.actionType).toBe('change_order.create');
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
    expect(w.emitted('update:visible').at(-1)).toEqual([false]);
  });

  it('queued (offline) result warns "Saved offline" and still closes', async () => {
    apiPostQueued.mockResolvedValueOnce({ queued: true, idempotency_key: 'k1' });
    const w = mountDialog();
    await w.get('[data-testid="mco-title"]').setValue('Extra hinge');
    await w.get('[data-testid="mco-submit"]').trigger('click');
    await flushPromises();

    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn', summary: 'Saved offline' })
    );
    expect(w.emitted('update:visible').at(-1)).toEqual([false]);
  });

  it('dirty form: Cancel asks before discarding; clean form closes silently', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const w = mountDialog();

    // Clean → closes without prompting.
    await w.get('[data-testid="mco-cancel"]').trigger('click');
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(w.emitted('update:visible').at(-1)).toEqual([false]);

    // Re-open, dirty it, decline the confirm → stays open.
    await w.setProps({ visible: true });
    await w.get('[data-testid="mco-title"]').setValue('Extra hinge');
    await w.get('[data-testid="mco-cancel"]').trigger('click');
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    // Last visibility event is still the earlier close; no new close emitted.
    const events = w.emitted('update:visible');
    expect(events.filter((e) => e[0] === false)).toHaveLength(1);
  });
});
