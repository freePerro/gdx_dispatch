/**
 * Email attachments picked in a composer — shared by EmailAttachmentPicker
 * and every send dialog that hosts it.
 *
 * A picked item is one of:
 *   { kind: 'upload', name, content_type, content_base64, size_bytes }  — from the device
 *   { kind: 'document' | 'job_photo' | 'estimate_pdf' | 'invoice_pdf', id, name, size_bytes? }
 * Stored things travel by id; the server loads the bytes and decides what the
 * sender may reach (core/email_attachments.py). Limits mirror that module.
 */

export const MAX_TOTAL_BYTES = 18_000_000
export const MAX_UPLOAD_BYTES = 15_000_000

// Phone photos are 3–8 MB straight off the camera. Two of them blow the
// email cap, and on a truck's signal they take minutes to upload. Anything
// bigger than this is re-encoded on the device to a size a customer's inbox
// shows just as well.
const DOWNSCALE_OVER_BYTES = 1_000_000
const MAX_EDGE_PX = 2048
const JPEG_QUALITY = 0.85

export function fmtBytes(n) {
  if (n == null || Number.isNaN(n)) return ''
  if (n < 1000) return `${n} B`
  if (n < 1_000_000) return `${Math.round(n / 1000)} KB`
  return `${(n / 1_000_000).toFixed(1)} MB`
}

/** Sum of the sizes we know. Rendered PDFs are sized by the server at send. */
export function knownTotalBytes(items) {
  return (items || []).reduce((sum, a) => sum + (Number(a.size_bytes) || 0), 0)
}

/** Stable identity for de-duplication: the same record picked twice is one attachment. */
export function attachmentKey(a) {
  return a.kind === 'upload' ? `upload:${a.name}:${a.size_bytes}` : `${a.kind}:${a.id}`
}

/** The wire shape the send endpoints take (AttachmentRef, extra=forbid). */
export function toAttachmentRefs(items) {
  return (items || []).map((a) =>
    a.kind === 'upload'
      ? { kind: 'upload', name: a.name, content_type: a.content_type || 'application/octet-stream', content_base64: a.content_base64 }
      : { kind: a.kind, id: String(a.id) },
  )
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const s = String(reader.result || '')
      resolve(s.slice(s.indexOf(',') + 1))
    }
    reader.onerror = () => reject(reader.error || new Error('Could not read the file'))
    reader.readAsDataURL(blob)
  })
}

/**
 * A downscaled JPEG of an image file, or null when it cannot (or need not) be
 * made smaller — HEIC on a browser that cannot decode it, a canvas the
 * browser refuses, or a re-encode that comes out no smaller.
 */
export async function downscaleImage(file, { maxEdge = MAX_EDGE_PX, quality = JPEG_QUALITY } = {}) {
  if (typeof createImageBitmap !== 'function' || typeof document === 'undefined') return null
  let bitmap
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  } catch {
    return null
  }
  try {
    const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height))
    const w = Math.max(1, Math.round(bitmap.width * scale))
    const h = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = w
    canvas.height = h
    const ctx = canvas.getContext('2d')
    if (!ctx) return null
    // JPEG has no alpha: paint white under a transparent PNG.
    ctx.fillStyle = '#fff'
    ctx.fillRect(0, 0, w, h)
    ctx.drawImage(bitmap, 0, 0, w, h)
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality))
    return blob && blob.size < file.size ? blob : null
  } finally {
    bitmap.close?.()
  }
}

function jpgName(name) {
  const base = String(name || 'photo').replace(/\.[^./\\]+$/, '')
  return `${base}.jpg`
}

/**
 * One device file → a picked 'upload' item. Large images are downscaled
 * first. Throws an Error with a user-facing message when the file is still
 * over the per-file limit.
 */
export async function readDeviceFile(file) {
  let blob = file
  let name = file.name || 'attachment'
  let type = file.type || 'application/octet-stream'
  let downscaled = false
  if (type.startsWith('image/') && file.size > DOWNSCALE_OVER_BYTES) {
    const small = await downscaleImage(file)
    if (small) {
      blob = small
      name = jpgName(name)
      type = 'image/jpeg'
      downscaled = true
    }
  }
  if (blob.size > MAX_UPLOAD_BYTES) {
    throw new Error(`${name} is ${fmtBytes(blob.size)}; one file can be at most ${fmtBytes(MAX_UPLOAD_BYTES)}.`)
  }
  return {
    kind: 'upload',
    name,
    content_type: type,
    content_base64: await blobToBase64(blob),
    size_bytes: blob.size,
    original_size_bytes: downscaled ? file.size : undefined,
  }
}

// The app answers every send failure with a reason that says whether the mail
// may have gone. Two failures carry no reason from the app: no status at all
// (the connection dropped after the request left — a phone losing signal) and
// a bare "Request failed (5xx)" (a proxy's page, not the app's answer; the app
// may still be sending behind it). Both can leave the outcome unknown, so say
// so rather than invite a duplicate send. It errs one way on purpose: a
// status-less error can also be a token refresh that failed before anything
// left, where the warning costs only a look in Sent Items.
export function sendErrorText(err, fallback = 'Send failed') {
  const msg = err?.message || ''
  const status = err?.status || 0
  if (!status || (status >= 500 && /^Request failed \(\d+\)$/.test(msg))) {
    return `${msg || fallback} — it may have gone. Check Sent Items before sending again.`
  }
  return msg || fallback
}
