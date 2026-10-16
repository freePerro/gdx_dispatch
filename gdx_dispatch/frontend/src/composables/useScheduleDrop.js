/**
 * First scheduling from a drop (Doug, 2026-10-16).
 *
 * A job with no date dropped onto a tech asks, before anything is written,
 * how many hours it takes and who the whole crew is, prefilled (initialCrew)
 * and open to adding anyone else. A job that already has a date just moves —
 * this is only for the drop that books it. Cancel writes nothing.
 */
import { ref } from 'vue';

// The PATCH body for an answer: the date, the crew with its lead, and the
// hours when they were changed from what the dialog showed.
export function scheduleDropPatch(answer, scheduledAt) {
  const patch = {
    scheduled_at: scheduledAt,
    assigned_tech_ids: answer.tech_ids,
    lead_tech_id: answer.lead,
  };
  if (answer.hours != null) patch.scheduled_duration_hours = answer.hours;
  return patch;
}

// The crew a dropped job starts the dialog with, matching what the drop did
// before the dialog: a job with one tech or none goes to the tech it was
// dropped on (a self-assigned job is handed over, not shared); a crew keeps
// its lead and members, and gains the dropped-on tech if they were not on it.
// `existingCrew` is lead first (crewOf).
export function initialCrew(techId, existingCrew = []) {
  const dropped = String(techId);
  const crew = existingCrew.map(String).filter(Boolean);
  if (crew.length < 2) return { techIds: [dropped], leadId: dropped };
  return { techIds: crew.includes(dropped) ? crew : [...crew, dropped], leadId: crew[0] };
}

// State for one ScheduleDropDialog. `ask` resolves { tech_ids, lead, hours }
// (hours is null when left as shown), or null on Cancel / close. A question
// still open when another is asked is answered Cancel.
export function useScheduleDropPrompt() {
  const prompt = ref({ visible: false, jobLabel: '', whenLabel: '', techIds: [], leadId: null, droppedId: null, hours: '' });
  let pending = null;

  function ask({ jobLabel, whenLabel, techId, crew = [], hours = null }) {
    if (pending) answer(null);
    const { techIds, leadId } = initialCrew(techId, crew);
    prompt.value = {
      visible: true,
      jobLabel: jobLabel || 'This job',
      whenLabel: whenLabel || '',
      techIds,
      leadId,
      droppedId: String(techId),
      hours: Number(hours) > 0 ? String(hours) : '',
    };
    return new Promise((resolve) => { pending = resolve; });
  }

  function answer(result) {
    const resolve = pending;
    pending = null;
    prompt.value = { ...prompt.value, visible: false };
    if (resolve) resolve(result);
  }

  return { prompt, ask, answer };
}
