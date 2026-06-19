/**
 * Dispatch — quickStatusChange completion routing (Phase 2 / C4).
 *
 * Doug 2026-05-10:
 *  - Phase 1 routed status='Complete' through POST /api/jobs/{id}/complete
 *    (the gated endpoint) so the silent-disappearing-job class was closed.
 *  - Phase 2 promotes completion from a status flip to a closeout
 *    transaction. The Status="Complete" affordance now OPENS the
 *    MobileJobCloseoutDialog instead of POSTing /complete directly. The
 *    dialog collects parts + hours + signature + notes and POSTs the
 *    /api/jobs/{id}/closeout endpoint (built in C2). Phase 1's /complete
 *    path remains alive but is no longer reached from dispatch.
 *
 * Pinned (post-C4):
 *  1. Complete: opens dialog (sets closeoutOpen=true, closeoutJob=job).
 *     No POST /complete, no POST /closeout from this handler.
 *  2. Other transitions (Scheduled / In Progress / Invoiced) keep using
 *     PATCH /api/jobs/{id}.
 *  3. Static-source guard — DispatchView mounts MobileJobCloseoutDialog
 *     and quickStatusChange opens it when status==='Complete'.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { defineComponent, ref } from 'vue';
import { mount, flushPromises } from '@vue/test-utils';

const apiPost = vi.fn();
const apiPatch = vi.fn();

// Re-implement quickStatusChange in isolation so the test pins the
// behavior contract, not the surrounding 1700-line DispatchView. A
// static-source guard at the bottom asserts the real DispatchView still
// matches the contract.
const Host = defineComponent({
  setup() {
    const closeoutOpen = ref(false);
    const closeoutJob = ref(null);
    const fetched = ref(0);
    async function fetchJobs() { fetched.value += 1; }

    async function quickStatusChange(job) {
      try {
        if (job.status === 'Complete') {
          closeoutJob.value = job;
          closeoutOpen.value = true;
        } else {
          await apiPatch(`/api/jobs/${job.id}`, { status: job.status });
        }
      } catch {
        await fetchJobs();
      }
    }

    return { quickStatusChange, closeoutOpen, closeoutJob, fetched };
  },
  template: '<div>{{ fetched }}</div>',
});

describe('Dispatch quickStatusChange — Phase 2 closeout routing', () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiPatch.mockReset();
  });

  it('opens the closeout dialog when status flips to Complete', async () => {
    const wrapper = mount(Host);
    await wrapper.vm.quickStatusChange({
      id: 'job-1',
      status: 'Complete',
      title: 'Broken spring',
      customer_name: 'Eric Wenz',
    });
    await flushPromises();

    expect(wrapper.vm.closeoutOpen).toBe(true);
    expect(wrapper.vm.closeoutJob).toEqual(expect.objectContaining({
      id: 'job-1',
      title: 'Broken spring',
      customer_name: 'Eric Wenz',
    }));
    // No HTTP traffic from the handler itself.
    expect(apiPost).not.toHaveBeenCalled();
    expect(apiPatch).not.toHaveBeenCalled();
  });

  it('uses PATCH for non-Complete status transitions', async () => {
    apiPatch.mockResolvedValue({});
    const wrapper = mount(Host);

    await wrapper.vm.quickStatusChange({ id: 'job-2', status: 'In Progress' });
    await wrapper.vm.quickStatusChange({ id: 'job-3', status: 'Scheduled' });
    await wrapper.vm.quickStatusChange({ id: 'job-4', status: 'Invoiced' });
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledTimes(3);
    expect(wrapper.vm.closeoutOpen).toBe(false);
  });

  it('DispatchView.vue opens the closeout dialog on Complete (static guard)', async () => {
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const SRC = readFileSync(
      join(__dirname, '..', 'DispatchView.vue'),
      'utf8',
    );

    // The handler must set closeoutOpen=true when status==='Complete'.
    const fnStart = SRC.indexOf('async function quickStatusChange');
    expect(fnStart).toBeGreaterThan(-1);
    const fnSpan = SRC.slice(fnStart, fnStart + 2000);
    expect(fnSpan).toMatch(/job\.status\s*===\s*['"]Complete['"]/);
    expect(fnSpan).toMatch(/closeoutOpen\.value\s*=\s*true/);
    // Must NOT POST /complete from this handler — Phase 2 moved that
    // responsibility to the closeout dialog.
    expect(fnSpan).not.toMatch(/api\.post\(\s*`\/api\/jobs\/\$\{[^}]+\}\/complete`/);

    // The dialog must be mounted in the template with v-model:visible
    // bound to closeoutOpen.
    expect(SRC).toMatch(/<MobileJobCloseoutDialog[\s\S]*?v-model:visible="closeoutOpen"/);
  });
});
