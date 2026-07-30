// Canonical job-type vocabulary — the JS mirror of core/job_taxonomy.py.
// Plan §9: two dropdowns (JobsView, CustomerDetailView) each carried their own
// list, disagreed on the service spelling, and left prod with four spellings
// of two work kinds. Both dropdowns now import THIS list, and a backend test
// (test_job_taxonomy.py) pins this file against the Python module so the two
// can never drift apart again.
//
// QB Import is deliberately absent — it is import provenance, not a choice.
export const JOB_TYPE_OPTIONS = [
  'Service Call',
  'Installation',
  'Repair',
  'Maintenance',
  'New Construction',
  'Inspection',
  'Other',
];

// The §8 pricing lane. Repair/Maintenance are service WORK (Doug 2026-07-29)
// — stored as themselves, priced hourly. Everything unrecognized goes to the
// office lane, which can never bill a customer wrong.
// The canonical service spelling — the default for field-created jobs.
export const DEFAULT_JOB_TYPE = 'Service Call';

export const SERVICE_LANE_TYPES = ['Service Call', 'Repair', 'Maintenance'];

export function isServiceLane(jobType) {
  return SERVICE_LANE_TYPES.includes(jobType);
}
