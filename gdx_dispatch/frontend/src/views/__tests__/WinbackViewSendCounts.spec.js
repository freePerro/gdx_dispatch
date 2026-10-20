/**
 * WinbackView — a campaign shows what its sends did (GDXA-1723).
 *
 * Send only queues rows; the drainer (tasks/winback_drain.py) delivers them
 * and the list API returns per-status `send_counts`. Without this line the
 * screen showed a bare "sent" badge for a campaign that had reached nobody.
 * Drives the real component against the list response.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { describe, expect, it, vi } from 'vitest';
import WinbackView from '../WinbackView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const CAMPAIGNS = [
  { id: 'c1', name: 'Spring', channel: 'sms', status: 'sending', send_counts: { queued: 2, sending: 1, sent: 4 } },
  { id: 'c2', name: 'Fall', channel: 'email', status: 'sent', send_counts: { sent: 40, failed: 1, skipped: 1, unknown: 3 } },
  { id: 'c3', name: 'Draft', channel: 'sms', status: 'draft', send_counts: {} },
  // Marked "sent" by the old send endpoint with rows still queued: never drained.
  { id: 'c4', name: 'Legacy', channel: 'sms', status: 'sent', send_counts: { queued: 40 } },
];

const apiGet = vi.fn((url) => {
  if (url === '/api/winback/campaigns') return Promise.resolve(CAMPAIGNS);
  if (url === '/api/winback/stats') return Promise.resolve({});
  return Promise.resolve([]);
});
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));

const RowScope = {
  props: ['row'],
  provide() {
    return { dtRow: this.row };
  },
  template: '<td><slot /></td>',
};

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  Column: {
    inject: { dtRow: { default: null } },
    template: '<span><slot name="body" :data="dtRow" /></span>',
  },
  DataTable: {
    props: ['value'],
    components: { RowScope },
    template: `<table><tbody>
        <tr v-for="(row, i) in (value || [])" :key="i" class="dt-row"><RowScope :row="row"><slot /></RowScope></tr>
      </tbody></table>`,
  },
  Badge: { props: ['value'], template: '<b class="badge">{{ value }}</b>' },
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
  Card: { template: '<div><slot name="content" /></div>' },
  Dialog: { template: '<div />' },
  ProgressSpinner: { template: '<div />' },
  EmptyState: { template: '<div />' },
  Select: { template: '<div />' },
  DatePicker: { template: '<div />' },
};

describe('WinbackView — send outcomes', () => {
  it('shows waiting, sent, not-sent and unconfirmed counts per campaign', async () => {
    const wrapper = mount(WinbackView, { global: { stubs } });
    await flushPromises();
    const rows = wrapper.findAll('tr.dt-row');
    expect(rows).toHaveLength(4);
    expect(rows[0].find('.badge').text()).toBe('sending');
    expect(rows[0].find('[data-test="send-summary"]').text()).toBe('3 waiting · 4 sent');
    expect(rows[1].find('[data-test="send-summary"]').text()).toBe('40 sent · 2 not sent · 3 unconfirmed');
    expect(rows[2].find('[data-test="send-summary"]').exists()).toBe(false);
    expect(rows[3].find('[data-test="send-summary"]').text()).toBe('40 not sent');
  });
});
