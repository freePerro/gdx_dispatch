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
