// Photos waiting to go out with a text — shared by the office messages page
// and the tech's phone screen.
//
// One photo per text (Doug, 2026-10-15): the carrier size limit is per
// message, so the server takes one photo per call and shrinks it to fit
// (modules/phone_com/mms.py). Several picked photos go as several texts, in
// order; the typed words ride with the first. The browser does no resizing —
// the server converts HEIC, turns photos upright and strips GPS, and doing it
// in one place means no client can skip it.
import { ref } from 'vue'

// Mirrors MAX_UPLOAD_BYTES in modules/phone_com/mms.py — refused here only so
// a 40MB pick fails at once instead of after the upload.
export const SMS_PHOTO_MAX_BYTES = 25 * 1024 * 1024
export const SMS_PHOTO_ACCEPT = 'image/*,.heic,.heif'

function isHeic(file) {
  return /^image\/hei[cf]/i.test(file.type || '') || /\.(heic|heif)$/i.test(file.name || '')
}

function looksLikePhoto(file) {
  // Windows Chrome hands HEIC over with an empty type; trust the extension then.
  return (file.type || '').startsWith('image/') || isHeic(file)
}

export function useSmsPhotos(api) {
  const photos = ref([]) // [{ file, preview }]
  const photoError = ref('')
  const progress = ref('')

  function add(fileList) {
    photoError.value = ''
    for (const file of Array.from(fileList || [])) {
      if (!looksLikePhoto(file)) {
        photoError.value = `${file.name || 'That file'} isn't a photo.`
        continue
      }
      if (file.size > SMS_PHOTO_MAX_BYTES) {
        photoError.value = `${file.name} is over 25MB.`
        continue
      }
      // HEIC has no browser preview outside Safari — an <img> of it is a broken
      // icon — so the chip shows its name instead. The server still converts it.
      let preview = ''
      if (!isHeic(file)) {
        try { preview = URL.createObjectURL(file) } catch { /* jsdom / old browsers */ }
      }
      photos.value.push({ file, preview })
    }
  }

  function remove(i) {
    const [p] = photos.value.splice(i, 1)
    if (p?.preview) URL.revokeObjectURL(p.preview)
  }

  function clear() {
    for (const p of photos.value) if (p.preview) URL.revokeObjectURL(p.preview)
    photos.value = []
    photoError.value = ''
    progress.value = ''
  }

  // Paste or drop: take the image files, leave text pastes alone.
  function addFromDataTransfer(dt) {
    const files = Array.from(dt?.files || []).filter(looksLikePhoto)
    if (files.length) add(files)
    return files.length > 0
  }

  // Sends each photo as its own text. Stops at the first failure and keeps the
  // unsent photos (and, if nothing went, the words) so the user can retry
  // without re-sending what already arrived. Returns { sent, wordsSent, error }.
  //
  // The batch is the list as it stood when Send was pressed: a photo pasted
  // or picked while it uploads waits for the next send, and never rides along
  // to this number.
  async function sendAll({ to, body, customerId, jobId }) {
    const batch = photos.value.slice()
    let sent = 0
    let words = (body || '').trim()
    for (const photo of batch) {
      progress.value = batch.length > 1 ? `Sending photo ${sent + 1} of ${batch.length}…` : 'Sending photo…'
      const fd = new FormData()
      fd.append('to', to)
      if (words) fd.append('body', words)
      if (customerId) fd.append('customer_id', customerId)
      if (jobId) fd.append('job_id', jobId)
      fd.append('file', photo.file, photo.file.name || 'photo.jpg')
      try {
        await api.post('/api/phone-com/messages/photo', fd)
      } catch (err) {
        progress.value = ''
        return { sent, wordsSent: sent > 0, error: err.message || 'Photo send failed' }
      }
      const i = photos.value.indexOf(photo)
      if (i >= 0) remove(i)
      words = ''
      sent += 1
    }
    progress.value = ''
    return { sent, wordsSent: true, error: null }
  }

  return { photos, photoError, progress, add, remove, clear, addFromDataTransfer, sendAll }
}
