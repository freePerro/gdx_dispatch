import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const failedRows = ref([]);
const retryMock = vi.fn();
const discardMock = vi.fn();
const toastAdd = vi.fn();
const confirmAsync = vi.fn();
const describe_ = (s) => (s === 413 ? 'The photo is too large for the server.' : s === 409 ? 'That job no longer exists on the server.' : 'The server refused this photo.');

vi.mock('../../composables/usePhotoQueue', () => ({
  usePhotoQueue: () => ({
    failedRows,
    retryFailedPhotos: retryMock,
    discardFailedPhotos: discardMock,
    describePhotoRefusal: describe_,
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync, confirmDestructive: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

import PhotoQueueFailedStrip from '../PhotoQueueFailedStrip.vue';

const row = (id, job_id, http_status) => ({ id, job_id, http_status, filename: `${id}.jpg`, error: null });

describe('PhotoQueueFailedStrip — the reader for refused photos (#525)', () => {
  beforeEach(() => {
    failedRows.value = [];
    retryMock.mockReset();
    discardMock.mockReset();
    toastAdd.mockReset();
    confirmAsync.mockReset();
  });

  it('renders nothing when no photo was refused', () => {
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    expect(w.find('[data-testid="photo-failed-strip"]').exists()).toBe(false);
  });

  it('says how many could not upload AND why — the only place a background refusal is ever explained', () => {
    failedRows.value = [row('p1', 'job-A', 413), row('p2', 'job-A', 413)];
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    expect(w.find('[data-testid="photo-failed-count"]').text()).toContain("2 photos couldn't upload");
    expect(w.find('[data-testid="photo-failed-reason"]').text()).toContain('too large');
  });

  it("is scoped to its job — job B's refused photo is not job A's problem to delete", () => {
    failedRows.value = [row('p1', 'job-B', 409)];
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    expect(w.find('[data-testid="photo-failed-strip"]').exists()).toBe(false);
  });

  it('with no jobId it reads phone-wide', () => {
    failedRows.value = [row('p1', 'job-B', 409), row('p2', 'job-C', 413)];
    const w = mount(PhotoQueueFailedStrip);
    expect(w.find('[data-testid="photo-failed-count"]').text()).toContain('2 photos');
  });

  it('Retry is scoped and reports success', async () => {
    failedRows.value = [row('p1', 'job-A', 409)];
    retryMock.mockResolvedValue({ retried: 1, uploaded: 1, refused: 0, pending: 0, status: null });
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    await w.find('[data-testid="photo-failed-retry"]').trigger('click');
    await flushPromises();
    expect(retryMock).toHaveBeenCalledWith({ jobId: 'job-A' });
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success', summary: 'Photo uploaded' }));
  });

  it('Retry that is refused again SAYS so, with the reason — a button must never visibly do nothing', async () => {
    failedRows.value = [row('p1', 'job-A', 413)];
    retryMock.mockResolvedValue({ retried: 1, uploaded: 0, refused: 1, pending: 0, status: 413 });
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    await w.find('[data-testid="photo-failed-retry"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn', summary: 'Still refused', detail: expect.stringMatching(/too large/) }));
  });

  it('Retry with no signal says it is waiting', async () => {
    failedRows.value = [row('p1', 'job-A', 413)];
    retryMock.mockResolvedValue({ retried: 1, uploaded: 0, refused: 0, pending: 1, status: null });
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    await w.find('[data-testid="photo-failed-retry"]').trigger('click');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn', summary: 'Waiting for signal' }));
  });

  it('Discard asks first, in words that say the phone holds the only copy — and does nothing on No', async () => {
    failedRows.value = [row('p1', 'job-A', 413)];
    confirmAsync.mockResolvedValue(false);
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    await w.find('[data-testid="photo-failed-discard"]').trigger('click');
    await flushPromises();
    expect(confirmAsync).toHaveBeenCalledWith(expect.objectContaining({
      header: expect.stringMatching(/Delete 1 photo that couldn't upload/),
      message: expect.stringMatching(/only on this phone/),
      acceptLabel: 'Delete',
    }));
    expect(discardMock).not.toHaveBeenCalled();
  });

  it('Discard deletes on Yes — scoped to this job — and says how many went', async () => {
    failedRows.value = [row('p1', 'job-A', 413), row('p2', 'job-A', 413), row('p3', 'job-A', 413)];
    confirmAsync.mockResolvedValue(true);
    discardMock.mockResolvedValue(3);
    const w = mount(PhotoQueueFailedStrip, { props: { jobId: 'job-A' } });
    await w.find('[data-testid="photo-failed-discard"]').trigger('click');
    await flushPromises();
    expect(confirmAsync).toHaveBeenCalledWith(expect.objectContaining({ header: expect.stringMatching(/3 photos/) }));
    expect(discardMock).toHaveBeenCalledWith({ jobId: 'job-A' });
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'info', summary: 'Deleted 3 photos from this phone' }));
  });
});
