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
// hours unless they are the job's own saved hours left as shown.
export function scheduleDropPatch(answer, scheduledAt) {
  const patch = {
    scheduled_at: scheduledAt,
    assigned_tech_ids: answer.tech_ids,
    lead_tech_id: answer.lead,
  };
  if (answer.hours != null) patch.scheduled_duration_hours = answer.hours;
  return patch;
}

// An estimate's man-hours over a crew, rounded up to the quarter hour and
// never under one (GDXA-1262): the figure a prompt prefills.
export function estimateHoursOver(manHours, crewSize) {
  const crew = Math.max(1, Number(crewSize) || 1);
  return Math.max(0.25, Math.ceil((Number(manHours) / crew) * 4) / 4);
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
// (hours is null when blank or left at the job's own saved hours), or null on
// Cancel / close. A question still open when another is asked is answered
// Cancel. `suggest`, given when the job has no hours of its own, resolves
// { manHours, accepted } or null; its answer reaches only the dialog it was
// asked for (`seq`), never one opened since.
export function useScheduleDropPrompt() {
  const prompt = ref({
    visible: false, seq: 0, jobLabel: '', whenLabel: '', techIds: [], leadId: null, droppedId: null, hours: '', estimate: null,
  });
  let pending = null;
  let seq = 0;

  function ask({ jobLabel, whenLabel, techId, crew = [], hours = null, suggest = null }) {
    if (pending) answer(null);
    const { techIds, leadId } = initialCrew(techId, crew);
    const mine = ++seq;
    prompt.value = {
      visible: true,
      seq: mine,
      jobLabel: jobLabel || 'This job',
      whenLabel: whenLabel || '',
      techIds,
      leadId,
      droppedId: String(techId),
      hours: Number(hours) > 0 ? String(hours) : '',
      estimate: null,
    };
    if (suggest) {
      Promise.resolve().then(suggest).then((estimate) => {
        if (!estimate || mine !== seq || !prompt.value.visible) return;
        prompt.value = { ...prompt.value, estimate };
      }, () => {});
    }
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
