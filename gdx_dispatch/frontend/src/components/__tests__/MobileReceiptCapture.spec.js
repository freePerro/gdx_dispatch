// Field receipt capture: draft Expense + hash-chained receipt photo in two
// calls, honest partial-failure messaging, and the everyone-can-file rule
// (no permission gating — the component must not consult the auth store).
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import MobileReceiptCapture from '../MobileReceiptCapture.vue'

const toastAdd = vi.fn()
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}))

const stubs = {
  Dialog: { template: '<div><slot /><slot name="footer" /></div>' },
  Button: {
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')"><slot /></button>',
    inheritAttrs: false,
  },
  InputNumber: { template: '<input />', props: ['modelValue'] },
  InputText: { template: '<input />', props: ['modelValue'] },
  Select: { template: '<select />', props: ['modelValue', 'options'] },
}

function mountIt() {
  return mount(MobileReceiptCapture, {
    global: { stubs, directives: { tooltip: {} } },
  })
}

function arm(wrapper, { amount = 55.25, vendor = 'Kwik Trip' } = {}) {
  const vm = wrapper.vm
  vm.form.amount = amount
  vm.form.vendor = vendor
  vm.file = new File(['x'], 'receipt.jpg', { type: 'image/jpeg' })
}

beforeEach(() => {
  toastAdd.mockClear()
  apiGet.mockReset()
  apiPost.mockReset()
  apiGet.mockResolvedValue(['Fuel', 'Parts/Supplies', 'Other'])
})

describe('MobileReceiptCapture', () => {
  it('requires photo + amount + vendor before save arms', async () => {
    const wrapper = mountIt()
    expect(wrapper.vm.canSave).toBe(false)
    arm(wrapper)
    expect(wrapper.vm.canSave).toBe(true)
    wrapper.vm.form.vendor = '  '
    expect(wrapper.vm.canSave).toBe(false)
  })

  it('creates a draft expense then attaches the photo as multipart', async () => {
    apiPost
      .mockResolvedValueOnce({ id: 'exp-1' }) // POST /api/expenses
      .mockResolvedValueOnce({ id: 'rcpt-1' }) // POST .../receipts
    const wrapper = mountIt()
    arm(wrapper)
    await wrapper.vm.save()

    expect(apiPost).toHaveBeenCalledTimes(2)
    const [expenseUrl, expenseBody] = apiPost.mock.calls[0]
    expect(expenseUrl).toBe('/api/expenses')
    expect(expenseBody.vendor).toBe('Kwik Trip')
    expect(expenseBody.amount).toBe(55.25)
    expect(expenseBody.category).toBe('Fuel')
    expect(expenseBody.date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    // No status field: the backend default 'draft' IS the review pipeline.
    expect(expenseBody.status).toBeUndefined()

    const [receiptUrl, receiptBody] = apiPost.mock.calls[1]
    expect(receiptUrl).toBe('/api/expenses/exp-1/receipts')
    expect(receiptBody).toBeInstanceOf(FormData)
    expect(receiptBody.get('file')).toBeInstanceOf(File)
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }))
    expect(wrapper.vm.visible).toBe(false)
  })

  it('photo-upload failure keeps the expense and says so loudly', async () => {
    apiPost
      .mockResolvedValueOnce({ id: 'exp-1' })
      .mockRejectedValueOnce(new Error('offline'))
    const wrapper = mountIt()
    arm(wrapper)
    await wrapper.vm.save()

    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn', summary: expect.stringContaining('photo failed') }),
    )
    expect(wrapper.vm.visible).toBe(false) // expense exists — don't trap the tech
  })

  it('expense-create failure keeps the dialog open with everything typed', async () => {
    apiPost.mockRejectedValueOnce(new Error('500'))
    const wrapper = mountIt()
    arm(wrapper)
    wrapper.vm.visible = true
    await wrapper.vm.save()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error' }))
    expect(wrapper.vm.visible).toBe(true)
    expect(wrapper.vm.form.vendor).toBe('Kwik Trip') // nothing wiped
  })

  it('falls back to built-in categories when the fetch fails', async () => {
    apiGet.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mountIt()
    await wrapper.vm.open()
    expect(wrapper.vm.form.category).toBe('Fuel')
  })
})
