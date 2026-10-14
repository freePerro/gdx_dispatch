import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Capacitor } from '@capacitor/core'
import { Network } from '@capacitor/network'
import { useOnlineState } from '../useOnlineState'

vi.mock('@capacitor/core', () => ({
  Capacitor: {
    isNativePlatform: vi.fn(),
  },
}))

vi.mock('@capacitor/network', () => ({
  Network: {
    getStatus: vi.fn(),
    addListener: vi.fn(),
  },
}))

describe('useOnlineState', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('exposes isOnline and connectionType', () => {
    Capacitor.isNativePlatform.mockReturnValue(false)
    const { isOnline, connectionType } = useOnlineState()
    expect(isOnline).toBeDefined()
    expect(connectionType).toBeDefined()
  })

  it('queries native Network status on native platform', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    Network.getStatus.mockResolvedValue({
      connected: true,
      connectionType: 'wifi',
    })

    const listeners = {}
    Network.addListener.mockImplementation((event, cb) => {
      listeners[event] = cb
      return Promise.resolve({ remove: vi.fn() })
    })

    const { isOnline, connectionType } = useOnlineState()

    // Simulate native status change
    if (listeners['networkStatusChange']) {
      listeners['networkStatusChange']({ connected: false, connectionType: 'none' })
      expect(isOnline.value).toBe(false)
      expect(connectionType.value).toBe('none')

      listeners['networkStatusChange']({ connected: true, connectionType: 'cellular' })
      expect(isOnline.value).toBe(true)
      expect(connectionType.value).toBe('cellular')
    }
  })
})
