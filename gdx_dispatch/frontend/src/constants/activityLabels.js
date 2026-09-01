/**
 * Audit-action → human label, shared by the dashboard "Recent Activity" card
 * and the dedicated Activity page.
 *
 * Lookup is keyed on `entity_type:action` FIRST, then bare `action`. Action
 * alone does not identify an event: `data_accessed` is a deliberately
 * entity-agnostic compliance action (see routers/customers.py — it is the
 * SOC2/GDPR read marker, not a customer-specific one). Today customers is its
 * only emit site, but the moment a job or invoice read logs the same action a
 * bare-action map would assert "Customer record viewed" about an invoice. The
 * scoped key keeps that honest; the bare key is the generic fallback.
 */
export const ACTIVITY_LABELS = {
  // ── entity-scoped (checked first) ──────────────────────────────────────
  'customer:data_accessed': 'Customer record viewed',

  // ── action-only ────────────────────────────────────────────────────────
  login: 'Signed in',
  login_success: 'Signed in',
  login_failed: 'Failed login attempt',
  logout: 'Signed out',
  token_refreshed: 'Session renewed',
  customer_created: 'New customer added',
  customer_updated: 'Customer updated',
  customer_deleted: 'Customer removed',
  job_created: 'New job created',
  job_updated: 'Job updated',
  job_completed: 'Job completed',
  estimate_created: 'New estimate created',
  estimate_updated: 'Estimate updated',
  estimate_accepted: 'Estimate accepted',
  estimate_customer_reassigned: 'Estimate moved to another customer',
  invoice_created: 'New invoice created',
  invoice_paid: 'Invoice paid',
  module_enabled: 'Module enabled',
  module_disabled: 'Module disabled',
  settings_updated: 'Settings changed',
  user_created: 'New user added',
  user_updated: 'User updated',

  // 2026-07-28 activity-attribution: the actions that actually dominate the
  // prod feed had no label and fell through to title-case + "(entity_type)",
  // which is where "Data Accessed (customer)" came from.
  data_accessed: 'Record viewed',
  patch_estimate: 'Estimate updated',
  patch_line: 'Estimate line updated',
  add_line: 'Estimate line added',
  delete_line: 'Estimate line removed',
  patch_invoice: 'Invoice updated',
  add_invoice_line: 'Invoice line added',
  invoice_line_patched: 'Invoice line updated',
  // "marked sent", not "sent": the mark-sent endpoint stamps status/sent_at,
  // it does not itself deliver an email. Claiming delivery would be false.
  invoice_marked_sent: 'Invoice marked sent',
  estimate_marked_sent: 'Estimate marked sent',
  estimate_duplicated: 'Estimate duplicated',
  // Estimate lifecycle (rejection-visibility plan, 2026-08-31). `rejected` is
  // written by ONE actor — the Outlook bounce detector — and means the email
  // never arrived; the label says so instead of echoing the enum word.
  estimate_sent: 'Estimate emailed to customer',
  estimate_send_failed: 'Estimate email failed to send',
  estimate_email_rejected: 'Estimate email bounced',
  estimate_resend_detected: 'Estimate re-sent from the mailbox',
  estimate_declined: 'Estimate declined',
  estimate_reopened: 'Estimate reopened',
  mobile_quote_built: 'Estimate built on mobile',
  mobile_quote_accepted: 'Estimate accepted (mobile)',
  mobile_quote_declined: 'Estimate declined (mobile)',
  public_estimate_accepted: 'Estimate accepted by customer (email link)',
  public_estimate_declined: 'Estimate declined by customer (email link)',
  invoice_email_rejected: 'Invoice email bounced',
  estimate_converted_to_job: 'Estimate converted to job',
  job_created_from_estimate: 'Job created from estimate',
  payment_recorded: 'Payment recorded',
  // "deleted", not "dismissed": the row is removed, not just hidden.
  notification_deleted: 'Notification deleted',
  notification_marked_read: 'Notification read',
  // NOT "New website lead": this action also fires from the authenticated
  // staff route (routers/leads.py create_landing_lead, requires leads.write),
  // so a lead typed in by the office would be labelled a website capture.
  landing_lead_created: 'Lead captured',
  landing_lead_deleted: 'Lead removed',
  qb_webhook_received: 'QuickBooks update received',

  // Customer-side activity. routers/portal.py has logged these all along;
  // they were rendering as raw action strings because nothing mapped them,
  // and they were mis-attributed to "Unknown user" until core/audit_labels.py
  // learned to resolve a CustomerUser actor.
  portal_login_verified: 'Customer signed in to the portal',
  portal_password_login: 'Customer signed in to the portal',
  portal_password_login_failed: 'Failed portal sign-in',
  portal_password_set: 'Customer set a portal password',
  portal_magic_link_sent: 'Portal sign-in link sent',
  portal_magic_link_send_failed: 'Portal sign-in link failed to send',
  portal_invite_sent: 'Portal invitation sent',
  portal_access_toggled: 'Portal access changed',
  portal_booking_created: 'Customer requested a booking',
  portal_message_sent: 'Customer sent a message',
  portal_estimate_accepted: 'Customer accepted the estimate',
  portal_estimate_declined: 'Customer declined the estimate',

  // Auth events that survive the feed's noise filter (core/activity_feed.py
  // hides only routine churn — token refresh, logout — and keeps everything
  // security-relevant).
  login_blocked: 'Sign-in blocked',
  failed_login: 'Failed sign-in attempt',
  password_reset_requested: 'Password reset requested',
  password_reset_success: 'Password reset completed',
  token_revoked: 'Session token revoked',
  user_sessions_revoked: 'All sessions revoked',
  refresh_replay_detected: 'Token replay detected',

  // CRUD verbs the generated audit blocks emit as bare create/update/delete
  // plus an entity type. Scoped keys so "Create Vendor (vendor)" stops
  // leaking through.
  'vendor:create_vendor': 'Vendor added',
  'vendor:update_vendor': 'Vendor updated',
  'vendor:delete_vendor': 'Vendor removed',
  'part:create_part': 'Part added',
  'part:update_part': 'Part updated',
  'part:delete_part': 'Part removed',
  'change_order:update_change_order': 'Change order updated',
  'po:update_po': 'Purchase order updated',
  communication_sent: 'Message sent',

  // Customer click-through on a public document link. Deliberately NOT called
  // an "open": this fires when someone follows the link we emailed, which is a
  // stronger signal than a tracking pixel (see core/customer_views.py).
  invoice_viewed_by_customer: 'Customer opened the invoice',
  estimate_viewed_by_customer: 'Customer opened the estimate',
};

export const ENTITY_ICONS = {
  auth: 'pi pi-sign-in',
  customer: 'pi pi-users',
  job: 'pi pi-briefcase',
  estimate: 'pi pi-file-edit',
  invoice: 'pi pi-dollar',
  module: 'pi pi-th-large',
  settings: 'pi pi-cog',
  user: 'pi pi-user',
};

/**
 * Human label for an audit row. Falls back to title-cased action plus the
 * entity type, which is what the whole feed used to do.
 */
export function formatActivityTitle(action, entityType) {
  if (!action) return 'Activity';
  const scoped = entityType ? ACTIVITY_LABELS[`${entityType}:${action}`] : undefined;
  if (scoped) return scoped;
  if (ACTIVITY_LABELS[action]) return ACTIVITY_LABELS[action];
  const label = action.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  if (entityType && entityType !== 'auth') return `${label} (${entityType})`;
  return label;
}
