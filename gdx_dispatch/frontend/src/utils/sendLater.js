/**
 * "Send later" times for a scheduled text (the server side is
 * modules/phone_com/scheduled.py).
 *
 * Times are the browser's local time — the person scheduling is in the
 * shop's timezone — and go to the server as an ISO string with an offset,
 * which it stores as UTC.
 */

// The server refuses anything sooner than a minute out.
const MIN_LEAD_MS = 60_000
// Past this the server holds a waiting text back rather than send it late
// (scheduled.py LATE_LIMIT).
const LATE_LIMIT_MS = 30 * 60_000

/** The next time the clock reads hour:minute, at least a minute from `now`. */
export function nextAt(hour, minute = 0, now = new Date()) {
  const d = new Date(now)
  d.setHours(hour, minute, 0, 0)
  if (d.getTime() < now.getTime() + MIN_LEAD_MS) d.setDate(d.getDate() + 1)
  return d
}

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** "Today 8:00 AM", "Tomorrow 8:00 AM", else "Sat, Oct 3, 8:00 AM". */
export function whenLabel(date, now = new Date()) {
  const d = date instanceof Date ? date : new Date(date)
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  if (sameDay(d, now)) return `Today ${time}`
  const tomorrow = new Date(now)
  tomorrow.setDate(tomorrow.getDate() + 1)
  if (sameDay(d, tomorrow)) return `Tomorrow ${time}`
  return `${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}, ${time}`
}

/** The quick picks: the next 8 AM and 9 AM. */
export function presets(now = new Date()) {
  return [nextAt(8, 0, now), nextAt(9, 0, now)].map((at) => ({ at, label: whenLabel(at, now) }))
}

/** After 8 PM or before 8 AM — when a text is best held for the morning. */
export function isQuietHours(now = new Date()) {
  const h = now.getHours()
  return h >= 20 || h < 8
}

/** What the operator reads for a scheduled text's state. */
export function scheduledStatusLabel(item, now = new Date()) {
  switch (item.status) {
    case 'scheduled': {
      // Past its time and still waiting means texting is down. The server
      // holds it back entirely once it is 30 minutes late, never sends late.
      const at = new Date(item.send_at)
      const lateMs = now.getTime() - at.getTime()
      if (lateMs > LATE_LIMIT_MS) return 'Overdue — will not send; send it now or reschedule'
      if (lateMs > 2 * 60_000) return 'Overdue — waiting to send'
      return `Sends ${whenLabel(at, now)}`
    }
    case 'sending': return 'Sending now…'
    case 'sent': return 'Sent'
    case 'canceled': return 'Canceled'
    case 'skipped': return 'Not sent'
    case 'failed': return 'Failed — not sent'
    case 'unknown': return 'Not confirmed — check the thread'
    default: return item.status
  }
}
