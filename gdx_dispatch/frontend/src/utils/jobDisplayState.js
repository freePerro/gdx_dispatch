/**
 * jobDisplayState — the SINGLE frontend reader for a job's canonical
 * display state (Slice 4 Wave 0b of sprint_job_lifecycle_terminal_states).
 *
 * The backend now emits an authoritative `display_state`
 * ({ stage, type, label, is_finished }) on every job payload (Wave 0a).
 * `type` is the Salesforce-style terminal tag — open | won | lost — so
 * "is this finished?" is structural, never a string match.
 *
 * Every surface (~49 of them) must read job state through THIS function
 * instead of re-deriving from `job.status` / `job.lifecycle_stage`. That
 * is the whole point of the sprint: one source of truth, no surface
 * inventing its own taxonomy or stopping at "Complete".
 *
 * Graceful fallback: payloads without `display_state` (older cache, an
 * enrichment failure that degraded to null) fall back to the legacy
 * `status`/`lifecycle_stage` string so nothing regresses — typed `open`,
 * neutral styling, never a fabricated terminal.
 *
 * Visual mapping is conventional, not a product decision:
 *   won   → success (green)   lost → danger (red)
 *   open  → info (blue)       overdue → warn (amber)
 */

const TYPE_SEVERITY = {
  won: 'success',
  lost: 'danger',
  open: 'info',
};

const TYPE_ICON = {
  won: 'pi pi-check-circle',
  lost: 'pi pi-times-circle',
  open: '',
};

// A few stages carry more meaning than their type alone — surface it.
const STAGE_OVERRIDE = {
  overdue: { severity: 'warn', icon: 'pi pi-exclamation-triangle' },
  partially_paid: { severity: 'warn', icon: '' },
  ready_to_bill: { severity: 'warn', icon: 'pi pi-file-edit' },
};

const SAFE_DEFAULT = Object.freeze({
  stage: 'unknown',
  type: 'open',
  label: 'Unknown',
  isFinished: false,
  severity: 'secondary',
  icon: '',
  unverified: true,
});

// The legacy values that ARE the deception this sprint exists to kill:
// "Complete" hides the entire billing tail. When we have no authoritative
// state we must NOT echo these as if they were a clean, done status —
// that reproduces the exact lie. They render explicitly unverified.
const _DECEPTIVE_LEGACY = new Set([
  'complete', 'completed', 'closed', 'done', 'finished',
]);

function titleize(s) {
  return String(s)
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

/**
 * @param {object|null|undefined} job - a job object from the API.
 * @returns {{stage:string,type:string,label:string,isFinished:boolean,severity:string,icon:string}}
 */
export function jobDisplayState(job) {
  if (!job || typeof job !== 'object') return { ...SAFE_DEFAULT };

  const ds = job.display_state;
  if (ds && typeof ds === 'object' && ds.label) {
    const type = ds.type === 'won' || ds.type === 'lost' ? ds.type : 'open';
    const stage = String(ds.stage || '').toLowerCase();
    const override = STAGE_OVERRIDE[stage] || null;
    return {
      stage: stage || 'unknown',
      type,
      label: String(ds.label),
      isFinished: ds.is_finished === true || type === 'won' || type === 'lost',
      severity: (override && override.severity) || TYPE_SEVERITY[type] || 'info',
      icon: override ? override.icon : TYPE_ICON[type] || '',
      unverified: false,
    };
  }

  // Fallback — no authoritative state on this payload (enrichment failed,
  // or this job came from an endpoint not yet emitting display_state).
  // NEVER fabricate a terminal, and NEVER echo the deceptive "Complete"
  // family as if it were a clean done state — that is the exact lie this
  // sprint kills. Everything here is flagged `unverified` so surfaces can
  // mark it non-authoritative.
  const legacy = job.status || job.lifecycle_stage || '';
  if (legacy) {
    const norm = String(legacy).toLowerCase().trim();
    if (_DECEPTIVE_LEGACY.has(norm)) {
      // We know work is done; we do NOT know the money state. Present it
      // as explicitly unverified, muted, NOT finished — not a clean
      // "Complete".
      return {
        stage: norm,
        type: 'open',
        label: `${titleize(legacy)} — sync pending`,
        isFinished: false,
        severity: 'secondary',
        icon: 'pi pi-question-circle',
        unverified: true,
      };
    }
    // Non-deceptive work stages (service_call/scheduled/estimate/…) are
    // still useful — show them, but flagged unverified.
    return {
      stage: norm,
      type: 'open',
      label: titleize(legacy),
      isFinished: false,
      severity: 'info',
      icon: '',
      unverified: true,
    };
  }
  return { ...SAFE_DEFAULT };
}

export default jobDisplayState;
