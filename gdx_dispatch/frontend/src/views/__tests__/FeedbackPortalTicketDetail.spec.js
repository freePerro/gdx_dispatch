/**
 * Feedback Portal — a ticket can be read and closed (#622).
 *
 * The list showed date / subject / category / status / priority / resolution
 * and nothing else: what the reporter typed (and the page + browser the bug
 * button appends) was stored in support_tickets.body and shown nowhere, and no
 * control could close a ticket — seven real bug reports sat "open" forever.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: getMock, post: postMock }),
}));
const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
const hasPermission = vi.fn(() => true);
vi.mock('../../stores/auth', () => ({ useAuthStore: () => ({ hasPermission }) }));

import FeedbackPortalView from '../FeedbackPortalView.vue';

const OPEN = {
  id: 't1', subject: 'Save button does nothing', category: 'bug', status: 'open', priority: 'high',
  created_at: '2026-08-06T14:02:00Z', closed_at: null, resolution_summary: null,
  opened_by_email: 'tech@example.com',
  body: 'Clicked Save on the estimate, nothing happened.\n\n---\nPage: /estimates/123\nBrowser: Chrome',
};
const CLOSED = {
  ...OPEN, id: 't2', subject: 'Old report', status: 'closed',
  closed_at: '2026-09-10T15:00:00Z', resolution_summary: 'Fixed in v1.118.8',
};

const STUBS = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  InputText: { template: '<input />' },
  Select: { props: ['modelValue', 'options'], template: '<select></select>' },
  Column: { template: '<col />' },
  Tag: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    inheritAttrs: false,
    template: `<textarea :data-testid="$attrs['data-testid']" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" />`,
  },
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    inheritAttrs: false,
    // Emits the native event like PrimeVue's Button, so the view's @click.stop works.
    template: `<button :data-testid="$attrs['data-testid']" :disabled="disabled" @click="$emit('click', $event)">{{ label }}</button>`,
  },
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  // Renders the #expansion slot for rows in expandedRows, like PrimeVue's.
  DataTable: {
    props: ['value', 'expandedRows'],
    emits: ['row-click', 'update:expandedRows'],
    template: `<table><tbody>
      <template v-for="row in value" :key="row.id">
        <tr class="dt-row" :data-row="row.id" @click="$emit('row-click', { data: row })"><td>{{ row.subject }}</td></tr>
        <tr v-if="expandedRows && expandedRows[row.id]"><td><slot name="expansion" :data="row" /></td></tr>
      </template>
    </tbody></table>`,
  },
};

async function mountWith(items) {
  getMock.mockResolvedValue({ items });
  const w = mount(FeedbackPortalView, { global: { stubs: STUBS } });
  await flushPromises();
  return w;
}

describe('FeedbackPortalView — read and close a ticket (#622)', () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset().mockResolvedValue({});
    toastAdd.mockReset();
    hasPermission.mockReset().mockReturnValue(true);
  });

  it('opening a row shows who reported it and exactly what they wrote', async () => {
    const w = await mountWith([OPEN]);
    expect(w.find('[data-testid="ticket-body"]').exists()).toBe(false);

    await w.get('[data-row="t1"]').trigger('click');
    const detail = w.get('[data-testid="ticket-detail-t1"]');
    expect(detail.text()).toContain('tech@example.com');
    const body = w.get('[data-testid="ticket-body"]');
    expect(body.element.tagName).toBe('PRE');
    expect(body.text()).toContain('Clicked Save on the estimate');
    expect(body.text()).toContain('Page: /estimates/123');
  });

  it('the office closes a ticket with a resolution', async () => {
    const w = await mountWith([OPEN]);
    await w.get('[data-row="t1"]').trigger('click');
    await w.get('[data-testid="ticket-close"]').trigger('click');

    const confirm = w.get('[data-testid="ticket-close-confirm"]');
    expect(confirm.attributes('disabled')).toBeDefined(); // nothing typed yet

    await w.get('[data-testid="ticket-resolution-input"]').setValue('  Fixed in v1.118.8  ');
    getMock.mockResolvedValue({ items: [{ ...OPEN, status: 'closed' }] });
    await w.get('[data-testid="ticket-close-confirm"]').trigger('click');
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith('/api/support/tickets/t1/close', {
      resolution_summary: 'Fixed in v1.118.8',
    });
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
    expect(getMock).toHaveBeenCalledTimes(2); // refreshed
    expect(w.find('.dlg').exists()).toBe(false);
  });

  it('a failed close says why and keeps the dialog open', async () => {
    const w = await mountWith([OPEN]);
    await w.get('[data-row="t1"]').trigger('click');
    await w.get('[data-testid="ticket-close"]').trigger('click');
    await w.get('[data-testid="ticket-resolution-input"]').setValue('Done');
    postMock.mockRejectedValue(Object.assign(new Error('HTTP 409'), { body: { detail: 'ticket is already closed' } }));
    await w.get('[data-testid="ticket-close-confirm"]').trigger('click');
    await flushPromises();

    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', detail: 'ticket is already closed' }));
    expect(w.find('.dlg').exists()).toBe(true);
  });

  it('a reporter reads their own report but cannot close it', async () => {
    hasPermission.mockReturnValue(false);
    const w = await mountWith([OPEN]); // the server sent the body: it is theirs
    await w.get('[data-row="t1"]').trigger('click');
    expect(w.find('[data-testid="ticket-body"]').exists()).toBe(true);
    expect(w.find('[data-testid="ticket-close"]').exists()).toBe(false);
    expect(hasPermission).toHaveBeenCalledWith('settings.write');
  });

  it("someone else's report the server withholds says so instead of showing a blank", async () => {
    hasPermission.mockReturnValue(false);
    const w = await mountWith([{ ...OPEN, body: null, opened_by_email: null }]);
    await w.get('[data-row="t1"]').trigger('click');
    expect(w.find('[data-testid="ticket-body"]').exists()).toBe(false);
    expect(w.get('[data-testid="ticket-body-withheld"]').text()).toContain('Only the team');
    expect(w.get('[data-testid="ticket-detail-t1"]').text()).not.toContain('Reported by');
  });

  it('a closed ticket shows its resolution instead of a close button', async () => {
    const w = await mountWith([CLOSED]);
    await w.get('[data-row="t2"]').trigger('click');
    expect(w.get('[data-testid="ticket-resolution"]').text()).toContain('Fixed in v1.118.8');
    expect(w.find('[data-testid="ticket-close"]').exists()).toBe(false);
  });
});
