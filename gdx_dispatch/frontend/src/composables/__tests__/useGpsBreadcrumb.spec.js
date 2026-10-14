import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useGpsBreadcrumb } from '../useGpsBreadcrumb'
import * as nativeGps from '../../lib/nativeGps'

const apiPost = vi.fn()
vi.mock('../useApi', () => ({
  useApi: () => ({
    post: apiPost,
  }),
}))

vi.mock('../../lib/nativeGps', () => ({
  getGpsPosition: vi.fn(),
  startForegroundGps: vi.fn(),
  stopForegroundGps: vi.fn(),
  isLocationDisclosureNeeded: vi.fn(() => false),
}))

describe('useGpsBreadcrumb', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.mocked(nativeGps.isLocationDisclosureNeeded).mockReturnValue(false)
    vi.mocked(nativeGps.startForegroundGps).mockResolvedValue({ running: true })
    vi.mocked(nativeGps.stopForegroundGps).mockResolvedValue({ running: false })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('samples position and posts breadcrumb payload to /api/mobile/location', async () => {
    vi.mocked(nativeGps.getGpsPosition).mockResolvedValue({
      coords: {
        latitude: 44.9778,
        longitude: -93.265,
        accuracy: 10,
        speed: 15.5,
        heading: 90,
      },
      timestamp: 1600000000000,
    })
    apiPost.mockResolvedValue({ id: 'loc-1', lat: 44.9778, lng: -93.265 })

    const gps = useGpsBreadcrumb({ getJobId: () => 'job-456' })
    const result = await gps.sampleOnce()

    expect(result).toEqual({ id: 'loc-1', lat: 44.9778, lng: -93.265 })
    expect(apiPost).toHaveBeenCalledWith(
      '/api/mobile/location',
      expect.objectContaining({
        lat: 44.9778,
        lng: -93.265,
        accuracy_m: 10,
        speed_mps: 15.5,
        heading_deg: 90,
        job_id: 'job-456',
      }),
      { suppressErrorToast: true }
    )
    expect(gps.lastLocation.value).toEqual({ id: 'loc-1', lat: 44.9778, lng: -93.265 })
    expect(gps.lastError.value).toBeNull()
  })

  it('starts foreground GPS service on start() and stops on stop()', async () => {
    vi.mocked(nativeGps.getGpsPosition).mockResolvedValue({
      coords: { latitude: 44.0, longitude: -93.0 },
      timestamp: Date.now(),
    })
    apiPost.mockResolvedValue({ id: 'loc-2' })

    const gps = useGpsBreadcrumb()
    gps.start()

    expect(gps.enabled.value).toBe(true)
    expect(nativeGps.startForegroundGps).toHaveBeenCalled()

    gps.stop()
    expect(gps.enabled.value).toBe(false)
    expect(nativeGps.stopForegroundGps).toHaveBeenCalled()
  })

  it('refuses to start when Google Play location disclosure is required', () => {
    vi.mocked(nativeGps.isLocationDisclosureNeeded).mockReturnValue(true)

    const gps = useGpsBreadcrumb()
    gps.start()

    expect(gps.enabled.value).toBe(false)
    expect(gps.lastError.value).toBe('location_disclosure_required')
    expect(nativeGps.startForegroundGps).not.toHaveBeenCalled()
  })

  it('stops sampling and shuts down foreground service on 403 Not Clocked In', async () => {
    vi.mocked(nativeGps.getGpsPosition).mockResolvedValue({
      coords: { latitude: 44.0, longitude: -93.0 },
      timestamp: Date.now(),
    })
    const err403 = new Error('Forbidden')
    err403.status = 403
    apiPost.mockRejectedValue(err403)

    const gps = useGpsBreadcrumb()
    gps.start()

    await vi.runOnlyPendingTimersAsync()

    expect(gps.enabled.value).toBe(false)
    expect(nativeGps.stopForegroundGps).toHaveBeenCalled()
  })
})
