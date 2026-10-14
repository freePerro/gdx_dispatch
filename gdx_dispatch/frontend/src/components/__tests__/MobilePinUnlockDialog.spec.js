import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import MobilePinUnlockDialog from '../MobilePinUnlockDialog.vue'
import * as nativeAuth from '../../lib/nativeAuth'

vi.mock('../../lib/nativeAuth', () => ({
  verifyQuickPin: vi.fn(),
  checkBiometricAvailability: vi.fn().mockResolvedValue({ available: false }),
  isBiometricEnabled: vi.fn().mockReturnValue(false),
  performBiometricUnlock: vi.fn(),
}))

describe('MobilePinUnlockDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(nativeAuth.checkBiometricAvailability).mockResolvedValue({ available: false })
    vi.mocked(nativeAuth.isBiometricEnabled).mockReturnValue(false)
  })

  it('renders keypad and dots when visible', () => {
    const wrapper = mount(MobilePinUnlockDialog, {
      props: { visible: true, autoPromptBiometric: false },
    })

    expect(wrapper.find('[data-test="mobile-pin-dialog"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pin-dots"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="key-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="key-0"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="btn-biometric-unlock"]').exists()).toBe(false)
  })

  it('renders fingerprint button when biometrics are enabled and available', async () => {
    vi.mocked(nativeAuth.isBiometricEnabled).mockReturnValue(true)
    vi.mocked(nativeAuth.checkBiometricAvailability).mockResolvedValue({ available: true })

    const wrapper = mount(MobilePinUnlockDialog, {
      props: { visible: true, autoPromptBiometric: false },
    })
    await flushPromises()

    expect(wrapper.find('[data-test="btn-biometric-unlock"]').exists()).toBe(true)
  })

  it('triggers biometric unlock when fingerprint button is clicked', async () => {
    vi.mocked(nativeAuth.isBiometricEnabled).mockReturnValue(true)
    vi.mocked(nativeAuth.checkBiometricAvailability).mockResolvedValue({ available: true })
    vi.mocked(nativeAuth.performBiometricUnlock).mockResolvedValue({ success: true })

    const wrapper = mount(MobilePinUnlockDialog, {
      props: { visible: true, autoPromptBiometric: false },
    })
    await flushPromises()

    await wrapper.find('[data-test="btn-biometric-unlock"]').trigger('click')

    expect(nativeAuth.performBiometricUnlock).toHaveBeenCalled()
    expect(wrapper.emitted('unlocked')).toBeTruthy()
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('enters digits and verifies pin on 4th digit', async () => {
    vi.mocked(nativeAuth.verifyQuickPin).mockResolvedValue(true)
    const wrapper = mount(MobilePinUnlockDialog, {
      props: { visible: true, autoPromptBiometric: false },
    })

    await wrapper.find('[data-test="key-1"]').trigger('click')
    await wrapper.find('[data-test="key-2"]').trigger('click')
    await wrapper.find('[data-test="key-3"]').trigger('click')
    await wrapper.find('[data-test="key-4"]').trigger('click')

    expect(nativeAuth.verifyQuickPin).toHaveBeenCalledWith('1234')
    expect(wrapper.emitted('unlocked')).toBeTruthy()
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('shows error on incorrect PIN', async () => {
    vi.mocked(nativeAuth.verifyQuickPin).mockResolvedValue(false)
    const wrapper = mount(MobilePinUnlockDialog, {
      props: { visible: true, autoPromptBiometric: false },
    })

    await wrapper.find('[data-test="key-9"]').trigger('click')
    await wrapper.find('[data-test="key-9"]').trigger('click')
    await wrapper.find('[data-test="key-9"]').trigger('click')
    await wrapper.find('[data-test="key-9"]').trigger('click')

    expect(nativeAuth.verifyQuickPin).toHaveBeenCalledWith('9999')
    expect(wrapper.emitted('unlocked')).toBeFalsy()
    expect(wrapper.find('[data-test="pin-error"]').exists()).toBe(true)
  })

  it('emits forgotPassword when forgot link clicked', async () => {
    const wrapper = mount(MobilePinUnlockDialog, {
      props: { visible: true, autoPromptBiometric: false },
    })

    await wrapper.find('[data-test="btn-forgot-pin"]').trigger('click')
    expect(wrapper.emitted('forgotPassword')).toBeTruthy()
  })
})
