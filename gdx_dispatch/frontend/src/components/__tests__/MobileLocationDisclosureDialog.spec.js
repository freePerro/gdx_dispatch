import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import MobileLocationDisclosureDialog from '../MobileLocationDisclosureDialog.vue'
import * as nativeGps from '../../lib/nativeGps'

describe('MobileLocationDisclosureDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('does not render when visible is false', () => {
    const wrapper = mount(MobileLocationDisclosureDialog, {
      props: { visible: false },
    })
    expect(wrapper.find('[data-test="mobile-location-disclosure-dialog"]').exists()).toBe(false)
  })

  it('renders prominent disclosure text when visible', () => {
    const wrapper = mount(MobileLocationDisclosureDialog, {
      props: { visible: true },
    })
    expect(wrapper.find('[data-test="mobile-location-disclosure-dialog"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('GDX Dispatch collects location data')
    expect(wrapper.text()).toContain('ONLY while you are clocked in on your active shift')
    expect(wrapper.text()).toContain('Shift-Bound Only')
  })

  it('acknowledges disclosure and emits accepted on accept button click', async () => {
    const spy = vi.spyOn(nativeGps, 'acknowledgeLocationDisclosure')
    const wrapper = mount(MobileLocationDisclosureDialog, {
      props: { visible: true },
    })

    await wrapper.find('[data-test="btn-accept-location"]').trigger('click')

    expect(spy).toHaveBeenCalled()
    expect(wrapper.emitted('accepted')).toBeTruthy()
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('emits declined and closes dialog on decline button click', async () => {
    const spy = vi.spyOn(nativeGps, 'acknowledgeLocationDisclosure')
    const wrapper = mount(MobileLocationDisclosureDialog, {
      props: { visible: true },
    })

    await wrapper.find('[data-test="btn-decline-location"]').trigger('click')

    expect(spy).not.toHaveBeenCalled()
    expect(wrapper.emitted('declined')).toBeTruthy()
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })
})
