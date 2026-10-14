import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import MobileInventoryView from '../MobileInventoryView.vue'
import * as nativeBarcode from '../../lib/nativeBarcode'

const mockApi = {
  get: vi.fn(),
}

vi.mock('../../composables/useApi', () => ({
  useApi: () => mockApi,
}))

vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: vi.fn() }),
}))

vi.mock('../../lib/nativeBarcode', () => ({
  isNativeBarcodeSupported: vi.fn(() => false),
  scanBarcode: vi.fn(),
}))

describe('MobileInventoryView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApi.get.mockResolvedValue([])
  })

  it('renders search input', () => {
    const wrapper = mount(MobileInventoryView, {
      global: {
        stubs: {
          Button: true,
          InputText: true,
        },
        directives: {
          tooltip: {},
        },
      },
    })
    expect(wrapper.find('[data-test="mi-search"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="mi-scan-btn"]').exists()).toBe(false)
  })

  it('renders scan button and populates search query on successful scan', async () => {
    vi.mocked(nativeBarcode.isNativeBarcodeSupported).mockReturnValue(true)
    vi.mocked(nativeBarcode.scanBarcode).mockResolvedValue({
      ok: true,
      value: 'SPRING-TORSION-207',
      format: 'CODE_128',
    })

    const wrapper = mount(MobileInventoryView, {
      global: {
        stubs: {
          Button: {
            template: '<button @click="$emit(\'click\')"><slot /></button>',
          },
          InputText: true,
        },
        directives: {
          tooltip: {},
        },
      },
    })

    expect(wrapper.find('[data-test="mi-scan-btn"]').exists()).toBe(true)
    await wrapper.find('[data-test="mi-scan-btn"]').trigger('click')

    expect(nativeBarcode.scanBarcode).toHaveBeenCalled()
    expect(wrapper.vm.searchQuery).toBe('SPRING-TORSION-207')
  })
})
