// QuickCaptureSheet — the Android share target funnels shared text into this
// sheet via the initialNote prop (AppBottomNav seeds it from ?share_* query
// params). Pin the seed-on-open behavior so a share never lands in a blank box.
import { describe, it, expect, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import QuickCaptureSheet from '../QuickCaptureSheet.vue';

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: vi.fn().mockResolvedValue({ items: [] }),
    post: vi.fn().mockResolvedValue({}),
  }),
}));

// Real PrimeVue Drawer teleports to <body> and animates — neither matters
// here. The stub renders slots inline and lets the test fire @show directly.
const DrawerStub = {
  name: 'Drawer',
  props: ['visible'],
  emits: ['show', 'update:visible'],
  template: '<div><slot /><slot name="footer" /></div>',
};

function mountSheet(props = {}) {
  return mount(QuickCaptureSheet, {
    props: { visible: true, ...props },
    global: { plugins: [PrimeVue], stubs: { Drawer: DrawerStub } },
  });
}

async function openSheet(wrapper) {
  wrapper.findComponent(DrawerStub).vm.$emit('show');
  await wrapper.vm.$nextTick();
  await wrapper.vm.$nextTick();
}

describe('QuickCaptureSheet initialNote seeding', () => {
  it('prefills the note box with initialNote when the sheet opens', async () => {
    const w = mountSheet({ initialNote: 'Wants 2 openers quoted\nhttps://example.com/opener' });
    await openSheet(w);
    expect(w.find('textarea').element.value).toBe(
      'Wants 2 openers quoted\nhttps://example.com/opener',
    );
  });

  it('opens blank without initialNote (manual FAB open unaffected)', async () => {
    const w = mountSheet();
    await openSheet(w);
    expect(w.find('textarea').element.value).toBe('');
  });

  it('reopening after a seed change picks up the new seed, not the old text', async () => {
    const w = mountSheet({ initialNote: 'first share' });
    await openSheet(w);
    await w.setProps({ initialNote: '' });
    await openSheet(w);
    expect(w.find('textarea').element.value).toBe('');
  });
});
