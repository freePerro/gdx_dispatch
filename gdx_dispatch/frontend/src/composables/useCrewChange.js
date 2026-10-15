/**
 * Crew-safe tech changes (Doug, 2026-10-15).
 *
 * The jobs PATCH reads a single `assigned_tech_id` as the whole crew, so on a
 * job with two or more techs any "pick a tech" control that sends one id
 * quietly drops everyone else. Every such control — the dispatch board's
 * drops and Reassign dialog, mobile dispatch's Reassign, the job page's
 * Technician picker — decides through here instead:
 *
 *   - fewer than two on the crew: the old single-tech path, unchanged;
 *   - a tech already on the crew: the crew stays as it is;
 *   - anyone else: ask whether to swap one member for them or add them
 *     (CrewChangeDialog), and Cancel changes nothing.
 */
import { ref } from 'vue';

// The crew, lead first. Takes a board/list job row (`assigned_tech_ids`,
// `lead_tech_id`, `technician_id` / `assigned_to`).
export function crewOf(job) {
  const crew = (job?.assigned_tech_ids || []).map(String).filter(Boolean);
  const primary = job?.technician_id || job?.assigned_to;
  if (primary && !crew.includes(String(primary))) crew.push(String(primary));
  const lead = String(job?.lead_tech_id || primary || '');
  return crew.includes(lead) ? [lead, ...crew.filter((t) => t !== lead)] : crew;
}

// The answers on offer when `techId` is picked for a job whose crew is
// `crew` (lead first). `firstId`, when on the crew, is offered first: the
// column a card was dragged out of. Each option carries the whole new crew
// and its lead; swapping the lead out makes the new tech lead.
export function crewChangeOptions(crew, techId, nameOf, firstId = null) {
  const lead = crew[0];
  const newId = String(techId);
  const newName = nameOf(newId);
  const members = firstId && crew.includes(String(firstId))
    ? [String(firstId), ...crew.filter((t) => t !== String(firstId))]
    : crew;
  const options = members.map((m) => ({
    key: `swap-${m}`,
    label: `Swap ${nameOf(m)} for ${newName}`,
    tech_ids: crew.map((t) => (t === m ? newId : t)),
    lead: m === lead ? newId : lead,
  }));
  options.push({ key: 'add', label: `Add ${newName} to the crew`, tech_ids: [...crew, newId], lead });
  return options;
}

// The PATCH body for a chosen option.
export function crewPatch(choice) {
  return { assigned_tech_ids: choice.tech_ids, lead_tech_id: choice.lead };
}

// State for one CrewChangeDialog. `ask` resolves the chosen option, or null
// on Cancel / close; a question still open when another is asked is
// answered Cancel, so nothing waits forever.
export function useCrewChangePrompt(nameOf) {
  const prompt = ref({ visible: false, header: '', jobLabel: '', crewLabel: '', options: [] });
  let pending = null;

  function ask({ crew, techId, jobLabel, firstId = null }) {
    if (pending) answer(null);
    prompt.value = {
      visible: true,
      header: `${nameOf(String(techId))} isn't on this crew`,
      jobLabel: jobLabel || 'This job',
      crewLabel: crew.map((t) => nameOf(t)).join(', '),
      options: crewChangeOptions(crew, techId, nameOf, firstId),
    };
    return new Promise((resolve) => { pending = resolve; });
  }

  function answer(choice) {
    const resolve = pending;
    pending = null;
    prompt.value = { ...prompt.value, visible: false };
    if (resolve) resolve(choice);
  }

  return { prompt, ask, answer };
}
