import { Capacitor } from '@capacitor/core'
import { Camera, CameraResultType, CameraSource } from '@capacitor/camera'

/**
 * Native Camera & Gallery picker wrapper for Capacitor Android / iOS.
 * Seamlessly integrates with existing File/Blob pipelines (IndexedDB, Dexie,
 * FormData) without breaking web/desktop browser fallback.
 */

export function isNativeCamera() {
  return Capacitor.isNativePlatform()
}

/**
 * Capture a photo using the device camera or pick from the photo library.
 * Returns a standard File object, or null if cancelled / dismissed.
 *
 * @param {Object} options
 * @param {string} [options.promptHeader='Add Photo'] Header on the prompt sheet
 * @param {CameraSource} [options.source=CameraSource.Prompt] Prompt, Camera, or Photos
 * @param {number} [options.quality=85] Compression quality (0-100)
 * @returns {Promise<File|null>}
 */
export async function captureNativePhoto({
  promptHeader = 'Add Photo',
  source = CameraSource.Prompt,
  quality = 85,
} = {}) {
  if (!isNativeCamera()) return null

  try {
    const photo = await Camera.getPhoto({
      quality,
      allowEditing: false,
      resultType: CameraResultType.Uri,
      source,
      promptLabelHeader: promptHeader,
      promptLabelPhoto: 'Choose from Gallery',
      promptLabelPicture: 'Take Photo',
    })

    if (!photo || !photo.webPath) return null

    const response = await fetch(photo.webPath)
    const blob = await response.blob()

    const ext = photo.format ? (photo.format.toLowerCase() === 'jpeg' ? 'jpg' : photo.format.toLowerCase()) : 'jpg'
    const mime = photo.format ? `image/${photo.format.toLowerCase()}` : 'image/jpeg'
    const filename = `photo_${Date.now()}.${ext}`

    return new File([blob], filename, { type: mime })
  } catch (err) {
    const msg = (err?.message || '').toLowerCase()
    // Gracefully handle user cancellation / dialog dismissal
    if (msg.includes('cancel') || msg.includes('dismiss') || msg.includes('closed') || err?.code === 'cancelled') {
      return null
    }
    throw err
  }
}
