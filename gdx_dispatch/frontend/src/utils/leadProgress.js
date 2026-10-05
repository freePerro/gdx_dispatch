/**
 * A lead's `progress` (GET /api/leads) is its selected estimate's job display
 * state — or a pre-job stage (Sold, Quoted, Estimate started, Declined,
 * Expired) in the same shape. JobStateChip reads a job's `display_state`, so
 * hand the progress over as one. `scheduled_at` rides along only when the
 * server sent it: the chip's "Awaiting Schedule" refinement keys off the
 * key's presence, and a missing key must never read as "no appointment".
 */
export function leadProgressAsJob(progress) {
  if (!progress || typeof progress !== 'object') return null;
  const job = { display_state: progress };
  if (Object.prototype.hasOwnProperty.call(progress, 'scheduled_at')) {
    job.scheduled_at = progress.scheduled_at;
  }
  return job;
}

export default leadProgressAsJob;
