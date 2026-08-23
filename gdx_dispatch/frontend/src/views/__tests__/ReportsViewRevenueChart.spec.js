/**
 * M8 (money-audit-2026-08-04) — "Revenue by Period" charted nothing.
 *
 * Two independent bugs, each hiding the other:
 *   1. the backend summed `total_amount`, NULL on every prod row  → revenue 0
 *   2. this view mapped `b.label` / `b.value`, fields the API has never
 *      emitted (it returns period_start / revenue)               → undefined
 *
 * So the chart drew an empty frame on a 0–1 axis with no x labels, while the
 * KPI card beside it showed real money. Fixing either half alone leaves the
 * chart blank, which is why this spec asserts the MAPPING, not just a request.
 *
 * There was no ReportsView spec at all before this file — the reason both bugs
 * survived to a prod browser walk on 2026-08-22.
 */
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const apiGet = vi.fn()

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
  useApiWithToast: () => ({ get: apiGet }),
}))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

// vue-chartjs must be mocked at the MODULE level: ReportsView imports Bar/Pie
// directly in <script setup>, so `global.stubs` never intercepts them and the
// real Chart.js runs — which dies in jsdom on canvas.getContext. Mocking here
// also lets the test read exactly what the chart was handed.
vi.mock('vue-chartjs', () => ({
  Bar: {
    name: 'BarStub',
    props: ['data', 'options'],
    template: '<div data-testid="bar-stub" />',
  },
  Pie: {
    name: 'PieStub',
    props: ['data', 'options'],
    template: '<div data-testid="pie-stub" />',
  },
}))

import ReportsView from '../ReportsView.vue'

// The real API shape, verified against prod on 2026-08-22.
const REVENUE_BY_PERIOD = {
  items: [
    { period_start: '2026-07-01T00:00:00+00:00', invoice_count: 15, revenue: 69741.68, avg_invoice: 4649.44 },
    { period_start: '2026-08-01T00:00:00+00:00', invoice_count: 23, revenue: 35101.02, avg_invoice: 1526.13 },
  ],
  period: 'month',
  total_revenue: 104842.7,
  range: { start: '2026-07-01', end: '2026-08-31' },
}

function routeGet(url) {
  if (url.includes('revenue-by-period')) return Promise.resolve(REVENUE_BY_PERIOD)
  if (url.includes('summary')) return Promise.resolve({ revenue_total: 104842.7, jobs_completed: 21, avg_job_value: 2060 })
  if (url.includes('top-customers')) return Promise.resolve({ items: [] })
  if (url.includes('sales-tax')) return Promise.resolve({ items: [], totals: {} })
  if (url.includes('/api/jobs')) return Promise.resolve({ items: [] })
  return Promise.resolve({})
}

const STUBS = { AppLayout: { template: '<div><slot /></div>' } }

async function mountView() {
  const wrapper = mount(ReportsView, { global: { stubs: STUBS } })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('ReportsView — revenue chart mapping (M8)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockImplementation(routeGet)
  })

  afterEach(() => vi.restoreAllMocks())

  it('feeds the chart real revenue numbers, not undefined', async () => {
    const wrapper = await mountView()
    const bar = wrapper.findComponent({ name: 'BarStub' })
    expect(bar.exists()).toBe(true)

    const data = bar.props('data')
    // The bug: [undefined, undefined]. Guard the values themselves.
    expect(data.datasets[0].data).toEqual([69741.68, 35101.02])
    expect(data.datasets[0].data.every((n) => typeof n === 'number')).toBe(true)
  })

  it('labels each bar from period_start, in UTC', async () => {
    const wrapper = await mountView()
    const data = wrapper.findComponent({ name: 'BarStub' }).props('data')

    expect(data.labels).toHaveLength(2)
    expect(data.labels.every((l) => typeof l === 'string' && l.length > 0)).toBe(true)
    // period_start is a UTC date_trunc boundary. Formatting it in local time
    // would render July as "Jun" for anyone west of UTC — i.e. everyone here.
    expect(data.labels[0]).toMatch(/Jul/)
    expect(data.labels[1]).toMatch(/Aug/)
  })

  it('never emits an undefined label or datapoint for a well-formed payload', async () => {
    const wrapper = await mountView()
    const data = wrapper.findComponent({ name: 'BarStub' }).props('data')

    expect(data.labels).not.toContain(undefined)
    expect(data.datasets[0].data).not.toContain(undefined)
  })

  it('shows the honest empty state when there is genuinely no data', async () => {
    apiGet.mockImplementation((url) =>
      url.includes('revenue-by-period')
        ? Promise.resolve({ items: [], period: 'month', total_revenue: 0 })
        : routeGet(url),
    )
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('No revenue data for this period.')
    expect(wrapper.find('[data-testid="bar-stub"]').exists()).toBe(false)
  })
})
