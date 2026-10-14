import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Capacitor } from '@capacitor/core'
import { BarcodeScanner, BarcodeFormat } from '@capacitor-mlkit/barcode-scanning'
import { isNativeBarcodeSupported, scanBarcode } from '../nativeBarcode.js'

vi.mock('@capacitor/core', () => ({
  Capacitor: {
    isNativePlatform: vi.fn(),
  },
}))

vi.mock('@capacitor-mlkit/barcode-scanning', () => ({
  BarcodeScanner: {
    checkPermissions: vi.fn(),
    requestPermissions: vi.fn(),
    isGoogleBarcodeScannerModuleAvailable: vi.fn(),
    installGoogleBarcodeScannerModule: vi.fn(),
    scan: vi.fn(),
  },
  BarcodeFormat: {
    QrCode: 'QR_CODE',
    Code128: 'CODE_128',
    Code39: 'CODE_39',
    Code93: 'CODE_93',
    Ean13: 'EAN_13',
    Ean8: 'EAN_8',
    UpcA: 'UPC_A',
    UpcE: 'UPC_E',
    DataMatrix: 'DATA_MATRIX',
  },
}))

describe('nativeBarcode', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('detects native platform status', () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    expect(isNativeBarcodeSupported()).toBe(true)

    Capacitor.isNativePlatform.mockReturnValue(false)
    expect(isNativeBarcodeSupported()).toBe(false)
  })

  it('returns unsupported on web platform', async () => {
    Capacitor.isNativePlatform.mockReturnValue(false)
    const res = await scanBarcode()
    expect(res).toEqual({ ok: false, reason: 'unsupported' })
    expect(BarcodeScanner.scan).not.toHaveBeenCalled()
  })

  it('handles permission denied cleanly', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    BarcodeScanner.checkPermissions.mockResolvedValue({ camera: 'prompt' })
    BarcodeScanner.requestPermissions.mockResolvedValue({ camera: 'denied' })

    const res = await scanBarcode()
    expect(res).toEqual({ ok: false, error: 'permission_denied' })
    expect(BarcodeScanner.scan).not.toHaveBeenCalled()
  })

  it('installs Google Barcode Scanner Module if not available', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    BarcodeScanner.checkPermissions.mockResolvedValue({ camera: 'granted' })
    BarcodeScanner.isGoogleBarcodeScannerModuleAvailable.mockResolvedValue({ available: false })
    BarcodeScanner.installGoogleBarcodeScannerModule.mockResolvedValue(undefined)
    BarcodeScanner.scan.mockResolvedValue({
      barcodes: [{ rawValue: 'SKU-SPRING-01', format: 'CODE_128' }],
    })

    const res = await scanBarcode()
    expect(BarcodeScanner.installGoogleBarcodeScannerModule).toHaveBeenCalled()
    expect(res).toEqual({
      ok: true,
      value: 'SKU-SPRING-01',
      format: 'CODE_128',
    })
  })

  it('handles user cancellation gracefully', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    BarcodeScanner.checkPermissions.mockResolvedValue({ camera: 'granted' })
    BarcodeScanner.isGoogleBarcodeScannerModuleAvailable.mockResolvedValue({ available: true })
    BarcodeScanner.scan.mockResolvedValue({ barcodes: [] })

    const res = await scanBarcode()
    expect(res).toEqual({ ok: false, cancelled: true })
  })

  it('catches dismiss/cancel exceptions as cancelled', async () => {
    Capacitor.isNativePlatform.mockReturnValue(true)
    BarcodeScanner.checkPermissions.mockResolvedValue({ camera: 'granted' })
    BarcodeScanner.isGoogleBarcodeScannerModuleAvailable.mockResolvedValue({ available: true })
    BarcodeScanner.scan.mockRejectedValue(new Error('User cancelled scan'))

    const res = await scanBarcode()
    expect(res).toEqual({ ok: false, cancelled: true })
  })
})
