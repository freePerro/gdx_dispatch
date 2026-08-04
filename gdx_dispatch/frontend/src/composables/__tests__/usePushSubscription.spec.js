import { describe, expect, it } from 'vitest'
import { _internal } from '../usePushSubscription'

describe('usePushSubscription internals', () => {
  describe('urlBase64ToUint8Array', () => {
    it('decodes a standard urlBase64 VAPID public key', () => {
      // Minimal valid urlBase64 (no padding, '-' / '_' substituted) that
      // round-trips with btoa.
      const raw = 'hello world!'
      const b64 = btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
      const out = _internal.urlBase64ToUint8Array(b64)
      const decoded = String.fromCharCode(...out)
      expect(decoded).toBe(raw)
    })

    it('handles missing padding', () => {
      // 'foo' encodes to 'Zm9v' (length 4, no padding) — already aligned.
      const out = _internal.urlBase64ToUint8Array('Zm9v')
      expect(String.fromCharCode(...out)).toBe('foo')
    })

    it('handles 1-byte input that needs 2 padding chars', () => {
      // 'a' → 'YQ' (len 2 → needs '==' padding)
      const out = _internal.urlBase64ToUint8Array('YQ')
      expect(String.fromCharCode(...out)).toBe('a')
    })

    it('decodes the urlsafe characters - and _', () => {
      // Bytes 0xfb, 0xff are 03 in standard b64 = '+/8' which becomes
      // '-_8' in urlsafe form.
      const out = _internal.urlBase64ToUint8Array('-_8')
      // First two bytes match what atob('+/8=') would give.
      const std = atob('+/8=')
      expect(out[0]).toBe(std.charCodeAt(0))
      expect(out[1]).toBe(std.charCodeAt(1))
    })
  })
})

// ── ensureSubscribed / fetchVapidPublicKey (2026-08-04 heal path) ──────────
// Prod shipped VAPID keys AFTER real devices had granted permission; the CTA
// only shows while permission is 'default', so those devices were stuck
// granted-but-unsubscribed. ensureSubscribed heals that state on app open.
import { afterEach, vi } from 'vitest'
import { ensureSubscribed, fetchVapidPublicKey } from '../usePushSubscription'

function stubPushEnv({ permission = 'granted', existingSub = null, subscribeResult = null } = {}) {
  const subscribeMock = vi.fn().mockResolvedValue(
    subscribeResult || { toJSON: () => ({ endpoint: 'https://push.example/ep', keys: { p256dh: 'k', auth: 'a' } }) },
  )
  const reg = {
    pushManager: {
      getSubscription: vi.fn().mockResolvedValue(existingSub),
      subscribe: subscribeMock,
    },
  }
  vi.stubGlobal('Notification', { permission, requestPermission: vi.fn() })
  vi.stubGlobal('PushManager', function PushManager() {})
  Object.defineProperty(global.navigator, 'serviceWorker', {
    configurable: true,
    value: {
      getRegistration: vi.fn().mockResolvedValue(reg),
      register: vi.fn().mockResolvedValue(reg),
    },
  })
  return { reg, subscribeMock }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ensureSubscribed', () => {
  it('does nothing when permission is not granted', async () => {
    stubPushEnv({ permission: 'default' })
    const api = { get: vi.fn(), post: vi.fn() }
    const r = await ensureSubscribed(api)
    expect(r).toMatchObject({ ok: false, healed: false, reason: 'not_granted' })
    expect(api.get).not.toHaveBeenCalled()
  })

  it('reports ok without healing when a subscription already exists', async () => {
    stubPushEnv({ existingSub: { endpoint: 'https://push.example/existing' } })
    const api = { get: vi.fn(), post: vi.fn() }
    const r = await ensureSubscribed(api)
    expect(r).toEqual({ ok: true, healed: false })
    expect(api.get).not.toHaveBeenCalled()
  })

  it('heals granted-but-unsubscribed: fetches key, subscribes, posts to backend', async () => {
    const { subscribeMock } = stubPushEnv({ existingSub: null })
    const api = {
      get: vi.fn().mockResolvedValue({ public_key: 'Zm9v' }),
      post: vi.fn().mockResolvedValue({}),
    }
    const r = await ensureSubscribed(api)
    expect(r).toMatchObject({ ok: true, healed: true })
    expect(subscribeMock).toHaveBeenCalledOnce()
    expect(api.post).toHaveBeenCalledWith('/api/push/v2/subscribe', expect.objectContaining({
      endpoint: 'https://push.example/ep',
    }))
  })

  it('reports the reason without healing when the backend has no key', async () => {
    stubPushEnv({ existingSub: null })
    const api = { get: vi.fn().mockResolvedValue({ public_key: '' }), post: vi.fn() }
    const r = await ensureSubscribed(api)
    expect(r).toMatchObject({ ok: false, healed: false, reason: 'no_vapid_key' })
    expect(api.post).not.toHaveBeenCalled()
  })
})

describe('fetchVapidPublicKey', () => {
  it('returns the key when configured', async () => {
    const api = { get: vi.fn().mockResolvedValue({ public_key: 'BBkey' }) }
    expect(await fetchVapidPublicKey(api)).toBe('BBkey')
  })

  it('returns empty string on error or missing key (CTA should not render)', async () => {
    expect(await fetchVapidPublicKey({ get: vi.fn().mockRejectedValue(new Error('401')) })).toBe('')
    expect(await fetchVapidPublicKey({ get: vi.fn().mockResolvedValue({}) })).toBe('')
  })
})
