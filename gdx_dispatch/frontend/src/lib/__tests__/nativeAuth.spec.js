import { describe, it, expect, beforeEach, vi } from 'vitest'
import { Capacitor } from '@capacitor/core'
import { NativeBiometric } from '@capgo/capacitor-native-biometric'
import {
  isQuickPinConfigured,
  isQuickPinEnabled,
  setQuickPin,
  verifyQuickPin,
  clearQuickPin,
  lockSession,
  unlockSession,
  isSessionLocked,
  checkBiometricAvailability,
  isBiometricEnabled,
  setBiometricEnabled,
  performBiometricUnlock,
} from '../nativeAuth.js'

vi.mock('@capacitor/core', () => ({
  Capacitor: {
    isNativePlatform: vi.fn(() => false),
  },
}))

vi.mock('@capgo/capacitor-native-biometric', () => ({
  NativeBiometric: {
    isAvailable: vi.fn(),
    verifyIdentity: vi.fn(),
  },
}))

describe('nativeAuth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(Capacitor.isNativePlatform).mockReturnValue(false)
  })

  it('reports not configured initially', () => {
    expect(isQuickPinConfigured()).toBe(false)
    expect(isQuickPinEnabled()).toBe(false)
    expect(isSessionLocked()).toBe(false)
  })

  it('rejects invalid PIN formats', async () => {
    await expect(setQuickPin('123')).rejects.toThrow('PIN must be exactly 4 digits')
    await expect(setQuickPin('abcd')).rejects.toThrow('PIN must be exactly 4 digits')
    await expect(setQuickPin('12345')).rejects.toThrow('PIN must be exactly 4 digits')
  })

  it('sets and verifies valid 4-digit PIN', async () => {
    await setQuickPin('1234')
    expect(isQuickPinConfigured()).toBe(true)
    expect(isQuickPinEnabled()).toBe(true)

    const wrong = await verifyQuickPin('9999')
    expect(wrong).toBe(false)

    const correct = await verifyQuickPin('1234')
    expect(correct).toBe(true)
  })

  it('manages session lock lifecycle', async () => {
    await setQuickPin('5678')
    expect(isSessionLocked()).toBe(false)

    lockSession()
    expect(isSessionLocked()).toBe(true)

    unlockSession()
    expect(isSessionLocked()).toBe(false)
  })

  it('clears PIN configuration', async () => {
    await setQuickPin('4321')
    clearQuickPin()
    expect(isQuickPinConfigured()).toBe(false)
    expect(isQuickPinEnabled()).toBe(false)
    expect(isSessionLocked()).toBe(false)
  })

  describe('Biometric / Fingerprint Authentication', () => {
    it('returns not_native when running on web', async () => {
      vi.mocked(Capacitor.isNativePlatform).mockReturnValue(false)
      const avail = await checkBiometricAvailability()
      expect(avail).toEqual({ available: false, reason: 'not_native' })

      const unlock = await performBiometricUnlock()
      expect(unlock).toEqual({ success: false, reason: 'not_native' })
    })

    it('checks availability on native platform', async () => {
      vi.mocked(Capacitor.isNativePlatform).mockReturnValue(true)
      vi.mocked(NativeBiometric.isAvailable).mockResolvedValue({
        isAvailable: true,
        biometryType: 1, // Fingerprint
      })

      const avail = await checkBiometricAvailability()
      expect(avail.available).toBe(true)
      expect(avail.biometryType).toBe(1)
    })

    it('toggles biometric enabled setting', async () => {
      await setQuickPin('1234')
      expect(isBiometricEnabled()).toBe(true)

      setBiometricEnabled(false)
      expect(isBiometricEnabled()).toBe(false)

      setBiometricEnabled(true)
      expect(isBiometricEnabled()).toBe(true)
    })

    it('performs successful biometric unlock', async () => {
      vi.mocked(Capacitor.isNativePlatform).mockReturnValue(true)
      vi.mocked(NativeBiometric.isAvailable).mockResolvedValue({ isAvailable: true })
      vi.mocked(NativeBiometric.verifyIdentity).mockResolvedValue(undefined)

      await setQuickPin('1234')
      lockSession()
      expect(isSessionLocked()).toBe(true)

      const result = await performBiometricUnlock()
      expect(result.success).toBe(true)
      expect(isSessionLocked()).toBe(false)
      expect(NativeBiometric.verifyIdentity).toHaveBeenCalled()
    })

    it('handles biometric cancellation gracefully', async () => {
      vi.mocked(Capacitor.isNativePlatform).mockReturnValue(true)
      vi.mocked(NativeBiometric.isAvailable).mockResolvedValue({ isAvailable: true })
      vi.mocked(NativeBiometric.verifyIdentity).mockRejectedValue(new Error('User cancelled authentication'))

      const result = await performBiometricUnlock()
      expect(result.success).toBe(false)
      expect(result.cancelled).toBe(true)
    })
  })
})
