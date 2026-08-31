/**
 * 2026-08-31 — useOfflineSync's mount hook awaits two IndexedDB reads before
 * it installs window/document listeners. If the host component unmounts
 * during those awaits, the continuation used to run anyway:
 *   - in the app: listeners re-added AFTER onUnmounted had removed them
 *     (a leak that fires syncNow() for a component that is gone);
 *   - in vitest: nothing unmounts these hosts — the jsdom environment is
 *     torn down after the file and its globals are DELETED, so the
 *     continuation threw "ReferenceError: window is not defined" as an
 *     unhandled rejection (invisible to CI: dangerouslyIgnoreUnhandledErrors).
 * These lock both guards: unmount-before-hydration installs nothing
 * (`disposed`); a simulated teardown throws nothing (`typeof window`); a
 * normal mount installs and a normal unmount removes.
 */
import 'fake-indexeddb/auto'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { defineComponent, h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { useOfflineSync } from '../useOfflineSync'
import { db } from '../../lib/offlineDb'

const Host = defineComponent({
  name: 'OfflineSyncHost',
  setup() {
    useOfflineSync()
    return () => h('div', 'host')
  },
})

async function settle(rounds = 8) {
  // The hook awaits two Dexie reads; each resolves on the microtask/IDB
  // task queue. Drain a few rounds so the continuation has definitely run.
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setTimeout(r, 0))
    await nextTick()
  }
}

let addSpy, removeSpy
beforeEach(async () => {
  await db.sync_queue.clear()
  await db.sync_metadata.clear()
  addSpy = vi.spyOn(window, 'addEventListener')
  removeSpy = vi.spyOn(window, 'removeEventListener')
})
afterEach(() => {
  addSpy.mockRestore()
  removeSpy.mockRestore()
})

const onlineAdds = () => addSpy.mock.calls.filter(([type]) => type === 'online').length
const onlineRemoves = () => removeSpy.mock.calls.filter(([type]) => type === 'online').length

describe('useOfflineSync — teardown during hydration', () => {
  it('unmounting before the awaited hydration finishes installs no listener', async () => {
    const wrapper = mount(Host)
    wrapper.unmount() // before the two awaits resolve
    await settle()
    expect(onlineAdds()).toBe(0)
  })

  it('a normal mount installs the online listener and unmount removes it', async () => {
    const wrapper = mount(Host)
    await settle()
    expect(onlineAdds()).toBe(1)
    wrapper.unmount()
    expect(onlineRemoves()).toBe(1)
  })

  it('survives the environment being torn down mid-hydration (what vitest does after a file)', async () => {
    // Nothing unmounts these hosts in specs: vitest tears the jsdom
    // environment down after the file and DELETES its globals, so the
    // awaited continuation used to throw "ReferenceError: window is not
    // defined" as an unhandled rejection. Simulate exactly that.
    const rejections = []
    const onRejection = (e) => rejections.push(e)
    process.on('unhandledRejection', onRejection)
    const savedWindow = globalThis.window
    const savedDocument = globalThis.document
    try {
      mount(Host) // never unmounted, like the real specs
      delete globalThis.window
      delete globalThis.document
      await settle()
    } finally {
      globalThis.window = savedWindow
      globalThis.document = savedDocument
      process.off('unhandledRejection', onRejection)
    }
    expect(rejections.map((e) => String(e))).toEqual([])
  })
})
