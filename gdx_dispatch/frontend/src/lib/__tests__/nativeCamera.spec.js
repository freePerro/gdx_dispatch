import { describe, it, expect, vi, beforeEach } from 'vitest'
import { isNativeCamera, captureNativePhoto } from '../nativeCamera'
import { Capacitor } from '@capacitor/core'
import { Camera, CameraSource, CameraResultType } from '@capacitor/camera'

vi.mock('@capacitor/core', () => ({
  Capacitor: {
    isNativePlatform: vi.fn(),
  },
}))

vi.mock('@capacitor/camera', () => ({
  Camera: {
    getPhoto: vi.fn(),
  },
  CameraSource: {
    Prompt: 'PROMPT',
    Camera: 'CAMERA',
    Photos: 'PHOTOS',
  },
  CameraResultType: {
    Uri: 'uri',
  },
}))

describe('nativeCamera', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    global.fetch = vi.fn()
  })

  it('reports native status based on Capacitor.isNativePlatform', () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    expect(isNativeCamera()).toBe(true)

    Capacitor.isNativePlatform.mockReturnValue(false)
    expect(isNativeCamera()).toBe(false)
  })

  it('returns null on non-native platform without calling Camera.getPhoto', async () => {
    Capacitor.isNativePlatform.mockReturnValue(false)
    const result = await captureNativePhoto()
    expect(result).toBeNull()
    expect(Camera.getPhoto).not.toHaveBeenCalled()
  })

  it('captures photo and returns a valid File object on native platform', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    Camera.getPhoto.mockResolvedValue({
      webPath: 'blob:https://localhost/uuid-1234',
      format: 'jpeg',
    })

    const mockBlob = new Blob(['mock-binary-data'], { type: 'image/jpeg' })
    global.fetch.mockResolvedValue({
      blob: vi.fn().mockResolvedValue(mockBlob),
    })

    const file = await captureNativePhoto({ promptHeader: 'Test Photo' })

    expect(Camera.getPhoto).toHaveBeenCalledWith(
      expect.objectContaining({
        quality: 85,
        source: CameraSource.Prompt,
        resultType: CameraResultType.Uri,
        promptLabelHeader: 'Test Photo',
      })
    )
    expect(file).toBeInstanceOf(File)
    expect(file.type).toBe('image/jpeg')
    expect(file.name).toMatch(/^photo_\d+\.jpg$/)
  })

  it('returns null silently when user cancels the camera dialog', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    Camera.getPhoto.mockRejectedValue(new Error('User cancelled photos app'))

    const file = await captureNativePhoto()
    expect(file).toBeNull()
  })

  it('throws on genuine hardware or permission errors', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    Camera.getPhoto.mockRejectedValue(new Error('Camera hardware unavailable'))

    await expect(captureNativePhoto()).rejects.toThrow('Camera hardware unavailable')
  })
})
