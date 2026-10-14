/**
 * Bell notification category → the page that answers it.
 *
 * Shared by the in-app bell (NotificationsDrawer) and the Android office
 * alerts (lib/officeAlerts.js), so a tapped phone notification lands where
 * the same bell row would. The Notification model carries no entity_id, so
 * these are list-level routes; an unmapped category returns null.
 */

export function destinationFor(category, { mobile = false } = {}) {
  if (!category) return null;
  switch (category) {
    case 'lead':
      return '/leads';
    case 'job':
    case 'job_assigned':
    case 'job_status':
      return mobile ? '/mobile/jobs' : '/jobs';
    case 'invoice':
    case 'invoice_paid':
    case 'invoice_overdue':
    // 'payment' is what core/office_notifications.notify_payment_received
    // writes when Stripe money lands. It belongs with the invoice cases:
    // the row names an invoice number and the office's next move is to open
    // it. Without this case the click fell through to `default` and merely
    // closed the drawer.
    case 'payment':
      return mobile ? '/mobile/billing' : '/invoices';
    case 'estimate':
    case 'estimate_signed':
      return mobile ? '/mobile/estimates' : '/estimates';
    case 'customer':
      return mobile ? '/mobile/customers' : '/customers';
    // The scheduled payroll send writes this, both when it sends and when it
    // holds. The held one is the whole point: it names the shifts to correct
    // and the correction is made on /timesheets. Desktop only — the office
    // fixes timesheets at a desk, and there is no mobile route to send to.
    case 'timesheet':
      return '/timesheets';
    case 'part':
    case 'part_shipped':
    case 'parts_to_order':
      return mobile ? '/mobile/parts-to-order' : '/purchase-orders';
    // A scheduled REPLY that did not go (modules/phone_com/scheduled.py) —
    // the thread shows what happened and is where to resend. Only the office
    // can schedule a reply (both SMS views need nav.office), though the bell
    // row reaches everyone, like the lead and payment broadcasts. Scheduled
    // invoice and estimate texts alert under 'invoice' / 'estimate' instead,
    // so an alert about a tech's own text opens a page a tech can reach.
    case 'sms':
      return mobile ? '/mobile/sms' : '/phone-com/messages';
    case 'inbox':
    case 'message':
      // Desktop goes to /inbox (InboxView -> /api/outlook/*), the real
      // Outlook-synced mailbox. It used to go to /communications, a screen
      // backed by an in-memory dict (`_EMAILS_BY_TENANT`) that emptied on
      // every container restart, so a message notification pointed at a
      // mailbox that could never be holding the message (#492). That screen
      // has since been removed outright (#350) and /communications now
      // redirects here. Mobile already went to the right place.
      return mobile ? '/mobile/inbox' : '/inbox';
    default:
      return null;
  }
}
