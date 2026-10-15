/**
 * plannerReminders.js — the planner's "Remind me" field, desktop and mobile.
 *
 * Owner Domain: jobs-dispatch (see .claude/agents/jobs-dispatch.md)
 *
 * A reminder is an instant. It goes out as ISO with an offset — the server
 * refuses a naive time, which a phone's local "09:00" would otherwise be read
 * as UTC — and is shown in the device's own time.
 */

// The DatePicker's Date → the API's remind_at (null clears it).
export function remindAtOut(d) {
  return d instanceof Date && !Number.isNaN(d.getTime()) ? d.toISOString() : null;
}

// The API's remind_at → a Date for the DatePicker.
export function remindAtIn(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

// "Oct 15, 9:00 AM", for a task card.
export function reminderLabel(iso) {
  const d = remindAtIn(iso);
  return d ? d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
}

// A card shows the bell until the reminder has gone off.
export function reminderPending(task) {
  return Boolean(task?.remind_at && !task.reminder_sent_at);
}
