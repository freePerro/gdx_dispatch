import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Capacitor } from '@capacitor/core'
import { Geolocation } from '@capacitor/geolocation'

const { mockGpsForeground } = vi.hoisted(() => ({
  mockGpsForeground: {
    start: vi.fn(),
    stop: vi.fn(),
    isRunning: vi.fn(),
    requestPermissions: vi.fn(),
  },
}))

vi.mock('@capacitor/core', () => ({
  Capacitor: {
    isNativePlatform: vi.fn(),
  },
  registerPlugin: vi.fn(() => mockGpsForeground),
}))

vi.mock('@capacitor/geolocation', () => ({
  Geolocation: {
    getCurrentPosition: vi.fn(),
  },
}))

import {
  isNativeGps,
  startForegroundGps,
  stopForegroundGps,
  isForegroundGpsRunning,
  getGpsPosition,
  hasAcknowledgedLocationDisclosure,
  acknowledgeLocationDisclosure,
  isLocationDisclosureNeeded,
} from '../nativeGps'

describe('nativeGps', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('detects native platform status', () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    expect(isNativeGps()).toBe(true)

    Capacitor.isNativePlatform.mockReturnValue(false)
    expect(isNativeGps()).toBe(false)
  })

  describe('Google Play Location Disclosure', () => {
    it('tracks disclosure acknowledgement in localStorage', () => {
      expect(hasAcknowledgedLocationDisclosure()).toBe(false)
      acknowledgeLocationDisclosure()
      expect(hasAcknowledgedLocationDisclosure()).toBe(true)
    })

    it('requires disclosure only on native platform when not yet acknowledged', () => {
      Capacitor.isNativePlatform.mockReturnValue(false)
      expect(isLocationDisclosureNeeded()).toBe(false)

      Capacitor.isNativePlatform.mockReturnValue(true)
      expect(isLocationDisclosureNeeded()).toBe(true)

      acknowledgeLocationDisclosure()
      expect(isLocationDisclosureNeeded()).toBe(false)
    })
  })

  describe('startForegroundGps', () => {
    it('returns { running: false } on web without calling native plugin', async () => {
      Capacitor.isNativePlatform.mockReturnValue(false)
      const res = await startForegroundGps()
      expect(res).toEqual({ running: false })
      expect(mockGpsForeground.start).not.toHaveBeenCalled()
    })

    it('calls native plugin start with custom title and text', async () => {
      Capacitor.isNativePlatform.mockReturnValue(true)
      mockGpsForeground.start.mockResolvedValue({ running: true })

      const res = await startForegroundGps({
        title: 'Custom Title',
        text: 'Custom Text',
      })
      expect(mockGpsForeground.start).toHaveBeenCalledWith({
        title: 'Custom Title',
        text: 'Custom Text',
      })
      expect(res).toEqual({ running: true })
    })

    it('gracefully catches plugin start errors', async () => {
      Capacitor.isNativePlatform.mockReturnValue(true)
      mockGpsForeground.start.mockRejectedValue(new Error('Permission denied'))

      const res = await startForegroundGps()
      expect(res.running).toBe(false)
      expect(res.error).toContain('Permission denied')
    })
  })

  describe('stopForegroundGps', () => {
    it('calls native plugin stop when running natively', async () => {
      Capacitor.isNativePlatform.mockReturnValue(true)
      mockGpsForeground.stop.mockResolvedValue({ running: false })

      const res = await stopForegroundGps()
      expect(mockGpsForeground.stop).toHaveBeenCalled()
      expect(res).toEqual({ running: false })
    })

    it('returns { running: false } on web without calling native plugin', async () => {
      Capacitor.isNativePlatform.mockReturnValue(false)
      const res = await stopForegroundGps()
      expect(res).toEqual({ running: false })
      expect(mockGpsForeground.stop).not.toHaveBeenCalled()
    })
  })

  describe('isForegroundGpsRunning', () => {
    it('queries native plugin when native', async () => {
      Capacitor.isNativePlatform.mockReturnValue(true)
      mockGpsForeground.isRunning.mockResolvedValue({ running: true })

      const running = await isForegroundGpsRunning()
      expect(running).toBe(true)
    })

    it('returns false on web', async () => {
      Capacitor.isNativePlatform.mockReturnValue(false)
      const running = await isForegroundGpsRunning()
      expect(running).toBe(false)
    })
  })

  describe('getGpsPosition', () => {
    it('uses Geolocation.getCurrentPosition on native platform', async () => {
      Capacitor.isNativePlatform.mockReturnValue(true)
      Geolocation.getCurrentPosition.mockResolvedValue({
        coords: {
          latitude: 45.123,
          longitude: -93.456,
          accuracy: 5,
          speed: 12.5,
          heading: 180,
        },
        timestamp: 1234567890,
      })

      const pos = await getGpsPosition()
      expect(pos.coords.latitude).toBe(45.123)
      expect(pos.coords.longitude).toBe(-93.456)
      expect(pos.coords.speed).toBe(12.5)
      expect(pos.timestamp).toBe(1234567890)
    })

    it('falls back to navigator.geolocation on web', async () => {
      Capacitor.isNativePlatform.mockReturnValue(false)
      const mockWebPos = {
        coords: { latitude: 44.0, longitude: -92.0, accuracy: 10 },
        timestamp: 9999,
      }
      global.navigator.geolocation = {
        getCurrentPosition: vi.fn((success) => success(mockWebPos)),
      }

      const pos = await getGpsPosition()
      expect(pos.coords.latitude).toBe(44.0)
      expect(pos.coords.longitude).toBe(-92.0)
    })
  })
})
