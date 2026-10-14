import { Capacitor } from '@capacitor/core'
import { BarcodeScanner, BarcodeFormat } from '@capacitor-mlkit/barcode-scanning'

export function isNativeBarcodeSupported() {
  return Capacitor.isNativePlatform()
}

/**
 * Scans a 1D barcode or 2D QR code using Google ML Kit Barcode Scanning.
 * Automatically checks and prompts for camera permission and downloads
 * the ML Kit model module if needed.
 *
 * @param {Object} options
 * @param {BarcodeFormat[]} [options.formats] Supported barcode formats
 * @returns {Promise<{ ok: boolean, value?: string, format?: string, cancelled?: boolean, error?: string }>}
 */
export async function scanBarcode(options = {}) {
  if (!isNativeBarcodeSupported()) {
    return { ok: false, reason: 'unsupported' }
  }

  try {
    // 1. Verify / request camera permissions
    let permStatus = await BarcodeScanner.checkPermissions()
    if (permStatus.camera === 'prompt' || permStatus.camera === 'prompt-with-rationale') {
      permStatus = await BarcodeScanner.requestPermissions()
    }

    if (permStatus.camera !== 'granted') {
      return { ok: false, error: 'permission_denied' }
    }

    // 2. Ensure Google Barcode Scanner Module is installed (Android)
    if (typeof BarcodeScanner.isGoogleBarcodeScannerModuleAvailable === 'function') {
      const { available } = await BarcodeScanner.isGoogleBarcodeScannerModuleAvailable()
      if (!available && typeof BarcodeScanner.installGoogleBarcodeScannerModule === 'function') {
        await BarcodeScanner.installGoogleBarcodeScannerModule()
      }
    }

    // 3. Launch native scanner overlay
    const formats = options.formats || [
      BarcodeFormat.QrCode,
      BarcodeFormat.Code128,
      BarcodeFormat.Code39,
      BarcodeFormat.Code93,
      BarcodeFormat.Ean13,
      BarcodeFormat.Ean8,
      BarcodeFormat.UpcA,
      BarcodeFormat.UpcE,
      BarcodeFormat.DataMatrix,
    ]

    const result = await BarcodeScanner.scan({ formats })
    const barcodes = result?.barcodes || []

    if (!barcodes.length) {
      return { ok: false, cancelled: true }
    }

    const first = barcodes[0]
    return {
      ok: true,
      value: first.rawValue || first.displayValue || '',
      format: first.format,
    }
  } catch (err) {
    const msg = (err?.message || '').toLowerCase()
    if (msg.includes('cancel') || msg.includes('dismiss') || msg.includes('back')) {
      return { ok: false, cancelled: true }
    }
    console.warn('[nativeBarcode] Scan failed:', err)
    return { ok: false, error: err?.message || String(err) }
  }
}
