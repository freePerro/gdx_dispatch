// One reading of a job's work stage for the phone views (GDXA-1942).
//
// The stage reaches the phone spelled three ways: the raw enum
// (Job.lifecycle_stage in tenant_models: "in_progress"), the /api/jobs list's
// display label (jobs._STATUS_CANON: "In Progress", "Complete", "Cancelled"),
// and the older free-text Job.status ("canceled", "completed"). Each view
// used to keep its own lookup table and matched only one spelling, so an
// "In Progress" job fell through to a neutral "pending" badge.

// Job.lifecycle_stage, in order.
export const JOB_STAGES = Object.freeze([
  'lead', 'service_call', 'estimate', 'scheduled', 'in_progress', 'completed', 'cancelled',
])

export const JOB_STAGE_LABELS = Object.freeze({
  lead: 'Lead',
  service_call: 'Service call',
  estimate: 'Estimate',
  scheduled: 'Scheduled',
  in_progress: 'In progress',
  completed: 'Completed',
  cancelled: 'Cancelled',
})

const ALIASES = { complete: 'completed', canceled: 'cancelled' }

// Lowercase, spaces and hyphens to "_", the two alternate spellings folded.
// Any word comes back keyed; callers decide which keys they accept.
export function jobStatusKey(value) {
  const s = String(value ?? '').trim().toLowerCase().replace(/[\s-]+/g, '_')
  return ALIASES[s] || s
}

// The lifecycle stage a value names, or '' when it names none.
export function normalizeJobStage(value) {
  const s = jobStatusKey(value)
  return JOB_STAGES.includes(s) ? s : ''
}

// Completed or cancelled: the work is over, whatever dispatch_status says.
export function isClosedStage(value) {
  const s = normalizeJobStage(value)
  return s === 'completed' || s === 'cancelled'
}
