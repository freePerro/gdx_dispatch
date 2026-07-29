/**
 * The audit-action label map is shared by DashboardView and ActivityView.
 * A wrong label is worse than no label — it asserts something untrue about
 * what happened — so the semantics are pinned here.
 */
import { describe, expect, it } from 'vitest';
import { ACTIVITY_LABELS, formatActivityTitle } from '../activityLabels';

describe('formatActivityTitle', () => {
  it('prefers the entity-scoped key over the bare action', () => {
    // data_accessed is an entity-agnostic compliance action. Scoped to a
    // customer read it is a customer record view; unscoped it must not claim
    // to know which record was read.
    expect(formatActivityTitle('data_accessed', 'customer')).toBe('Customer record viewed');
    expect(formatActivityTitle('data_accessed', 'invoice')).toBe('Record viewed');
    expect(formatActivityTitle('data_accessed', undefined)).toBe('Record viewed');
  });

  it('falls back to title-case plus entity type for unmapped actions', () => {
    expect(formatActivityTitle('widget_frobnicated', 'widget')).toBe(
      'Widget Frobnicated (widget)',
    );
  });

  it('omits the entity suffix for auth rows', () => {
    expect(formatActivityTitle('password_rotated', 'auth')).toBe('Password Rotated');
  });

  it('handles a missing action without throwing', () => {
    expect(formatActivityTitle(undefined, 'job')).toBe('Activity');
    expect(formatActivityTitle('', 'job')).toBe('Activity');
  });

  it('does not overclaim delivery for mark-sent actions', () => {
    // The mark-sent endpoints stamp status/sent_at; they do not themselves
    // deliver an email. "Invoice sent" would be a false statement.
    expect(ACTIVITY_LABELS.invoice_marked_sent).toBe('Invoice marked sent');
    expect(ACTIVITY_LABELS.estimate_marked_sent).toBe('Estimate marked sent');
  });

  it('does not describe a deleted notification as merely dismissed', () => {
    expect(ACTIVITY_LABELS.notification_deleted).toBe('Notification deleted');
  });

  it('labels lead capture neutrally across both emit sites', () => {
    // Fires from the public website form AND the authenticated staff route.
    expect(ACTIVITY_LABELS.landing_lead_created).toBe('Lead captured');
  });
});

describe('customer-side portal actions', () => {
  it('names portal events instead of leaking raw action strings', () => {
    // routers/portal.py has written these all along; nothing mapped them, so
    // the audit page rendered "Portal Login Verified (customer_user)".
    expect(formatActivityTitle('portal_login_verified', 'customer_user')).toBe(
      'Customer signed in to the portal',
    );
    expect(formatActivityTitle('portal_estimate_accepted', 'estimate')).toBe(
      'Customer accepted the estimate',
    );
    expect(formatActivityTitle('portal_booking_created', 'booking_request')).toBe(
      'Customer requested a booking',
    );
  });

  it('distinguishes a customer accepting an estimate from staff doing it', () => {
    expect(formatActivityTitle('portal_estimate_accepted', 'estimate')).not.toBe(
      formatActivityTitle('estimate_accepted', 'estimate'),
    );
  });
});
