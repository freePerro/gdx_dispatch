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
