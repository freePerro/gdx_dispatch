<template>
  <div class="today-panel" :class="{ compact }" data-testid="today-panel">
    <div class="today-head">
      <h3 class="today-date">{{ dayLabel }}</h3>
    </div>

    <!-- NOTES — loose thoughts that aren't tasks yet -->
    <section class="today-notes">
      <div class="section-row">
        <label for="today-note" class="section-label">Notes</label>
        <span class="note-status" data-testid="today-note-status">{{ noteStatus }}</span>
      </div>
      <Textarea
        id="today-note"
        v-model="draft"
        autoResize
        rows="3"
        :maxlength="NOTE_MAX"
        class="w-full"
        placeholder="Loose thoughts for today — not tasks yet"
        :disabled="loading"
        data-testid="today-note"
        @blur="saveNote"
      />
      <Button
        v-if="saveState === 'error'"
        label="Not saved — retry"
        icon="pi pi-refresh"
        severity="danger"
        size="small"
        text
        data-testid="today-note-retry"
        @click="saveNote"
      />
      <div v-if="conflict" class="note-conflict" role="alert" data-testid="today-note-conflict">
        <p class="conflict-lead">{{ conflictLead }}</p>
        <div class="conflict-cols">
          <div>
            <div class="conflict-label">Latest{{ conflictBy }}</div>
            <pre class="conflict-text">{{ conflict.current.body || '(empty)' }}</pre>
          </div>
          <div>
            <div class="conflict-label">Yours</div>
            <pre class="conflict-text">{{ draft || '(empty)' }}</pre>
          </div>
        </div>
        <div class="conflict-actions">
          <Button label="Use latest" size="small" outlined data-testid="today-note-use-latest" @click="useLatest" />
          <Button label="Keep mine" size="small" data-testid="today-note-keep-mine" @click="keepMine" />
        </div>
      </div>
    </section>

    <!-- QUICK ADD -->
    <form class="today-add" @submit.prevent="quickAdd">
      <InputText
        v-model="quickTitle"
        class="flex-1"
        maxlength="300"
        placeholder="Add a task for today"
        aria-label="Add a task for today"
        data-testid="today-add-input"
      />
      <Button type="submit" icon="pi pi-plus" label="Add" :loading="adding" :disabled="!quickTitle.trim()" data-testid="today-add" />
    </form>

    <!-- TODAY'S TASKS -->
    <div v-if="loading" class="today-empty">Loading…</div>
    <div v-else-if="!tasks.length" class="today-empty" data-testid="today-empty">
      Nothing on Today yet. Add a task above, or pick one from the suggestions.
    </div>
    <ul v-else class="today-list" data-testid="today-list">
      <li v-for="t in tasks" :key="t.id" class="today-row" :class="[t.priority, t.status]" data-testid="today-task">
        <Checkbox v-model="t._done" :binary="true" :aria-label="`Mark ${t.title} done`" @change="toggleDone(t)" />
        <button type="button" class="row-body" @click="emit('open-task', t)">
          <span class="row-title" :class="{ done: t.status === 'done' }">{{ t.title }}</span>
          <span class="row-meta">
            <Tag v-if="t.source === 'ai'" value="AI" severity="info" class="ai-tag" />
            <span v-if="t.priority === 'urgent'" class="pill pill-danger">URGENT</span>
            <span v-else-if="t.priority === 'high'" class="pill pill-warn">HIGH</span>
            <span v-if="t.due_date" class="meta-item"><i class="pi pi-calendar" /> Due {{ shortDate(t.due_date) }}</span>
          </span>
        </button>
        <Button
          icon="pi pi-times"
          text
          rounded
          severity="secondary"
          class="row-action"
          :aria-label="`Remove ${t.title} from Today`"
          data-testid="today-remove"
          @click="pin(t, false)"
        />
      </li>
    </ul>

    <!-- SUGGESTIONS -->
    <section v-if="carried.length || due.length" class="today-suggest" data-testid="today-suggestions">
      <h4 class="section-label">Suggestions</h4>
      <template v-for="group in suggestionGroups" :key="group.key">
        <div v-if="group.items.length" class="suggest-group">
          <div class="suggest-title">{{ group.title }}</div>
          <ul class="today-list">
            <li v-for="t in group.items" :key="t.id" class="today-row suggest" :data-testid="`suggest-${group.key}`">
              <button type="button" class="row-body" @click="emit('open-task', t)">
                <span class="row-title">{{ t.title }}</span>
                <span class="row-meta">
                  <Tag v-if="t.source === 'ai'" value="AI" severity="info" class="ai-tag" />
                  <span v-if="group.key === 'carried'" class="meta-item"><i class="pi pi-history" /> On {{ shortDate(t.today_date) }}</span>
                  <span v-else class="meta-item" :class="{ overdue: isOverdue(t) }">
                    <i class="pi pi-calendar" /> {{ isOverdue(t) ? 'Overdue' : 'Due' }} {{ shortDate(t.due_date) }}
                  </span>
                </span>
              </button>
              <Button
                icon="pi pi-plus"
                text
                rounded
                class="row-action"
                :aria-label="`Add ${t.title} to Today`"
                data-testid="today-suggest-add"
                @click="pin(t, true)"
              />
            </li>
          </ul>
        </div>
      </template>
      <button v-if="dueMore" type="button" class="more-link" data-testid="today-due-more" @click="emit('show-tasks')">
        and {{ dueMore }} more in My Tasks
      </button>
    </section>
  </div>
</template>

<script setup>
// The Planner's Today tab (the 2026-09-28 planner Today plan), shared by the
// desktop and mobile planners. Tasks pinned to today, a notes box, and
// suggestions (unfinished recent pins, due/overdue). The server owns "today".
//
// The note saves with the version it last saw; a 409 means someone else (the
// AI, another tab) saved in between, and both texts are shown so nothing is
// dropped without the user choosing.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useApiWithToast } from '../composables/useApiWithToast'
import { formatDate, formatTime } from '../composables/useFormatters'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

defineProps({ compact: { type: Boolean, default: false } })
const emit = defineEmits(['open-task', 'changed', 'show-tasks'])
const api = useApiWithToast()

const NOTE_MAX = 5000
const SAVE_DELAY_MS = 2000

const loading = ref(true)
const day = ref(null)
const tasks = ref([])
const carried = ref([])
const due = ref([])
const dueMore = ref(0)
// The server's copy — its updated_at is the version every save is based on.
const note = ref({ date: null, body: '', updated_at: null, updated_via: null })
const draft = ref('')
const saveState = ref('idle') // idle | saving | saved | error
const conflict = ref(null) // { reason, current }
const quickTitle = ref('')
const adding = ref(false)
let timer = null

const dirty = computed(() => draft.value !== note.value.body)

const dayLabel = computed(() => {
  if (!day.value) return 'Today'
  return formatDate(day.value, { options: { weekday: 'long', month: 'short', day: 'numeric' } })
})

const noteStatus = computed(() => {
  if (saveState.value === 'saving') return 'Saving…'
  if (conflict.value) return 'Not saved'
  if (dirty.value) return ''
  if (note.value.updated_at && note.value.updated_via === 'ai') {
    return `Edited by AI at ${formatTime(note.value.updated_at)}`
  }
  if (saveState.value === 'saved') return 'Saved'
  return ''
})

const conflictLead = computed(() => (conflict.value?.reason === 'day_changed'
  ? "It's a new day — this page still held yesterday's note."
  : 'This note was changed somewhere else while you were typing.'))
const conflictBy = computed(() => {
  const c = conflict.value?.current
  if (!c?.updated_at) return ''
  return ` (${c.updated_via === 'ai' ? 'by AI ' : ''}at ${formatTime(c.updated_at)})`
})

const suggestionGroups = computed(() => [
  { key: 'carried', title: 'Unfinished from earlier', items: carried.value },
  { key: 'due', title: 'Due / overdue', items: due.value },
])

function shortDate(d) {
  if (!d) return ''
  const out = formatDate(d, { options: { month: 'short', day: 'numeric' } })
  return out === '—' ? '' : out
}
function isOverdue(t) { return Boolean(t.due_date && day.value && t.due_date < day.value) }

const withDone = (list) => (list || []).map((t) => ({ ...t, _done: t.status === 'done' }))

async function load() {
  try {
    const data = await api.get('/api/planner/today')
    day.value = data?.date || null
    tasks.value = withDone(data?.tasks)
    carried.value = data?.carried || []
    due.value = data?.due || []
    dueMore.value = data?.due_more || 0
    // Never move the base version under unsaved typing: the next save would
    // then be "based on" text the user never saw and overwrite it silently.
    if (data?.note && !dirty.value && !conflict.value) {
      note.value = data.note
      draft.value = data.note.body || ''
    }
  } catch {
    /* toast handled */
  } finally {
    loading.value = false
  }
}

function schedule() {
  clearTimeout(timer)
  timer = setTimeout(saveNote, SAVE_DELAY_MS)
}

async function saveNote() {
  clearTimeout(timer)
  if (!dirty.value || conflict.value || saveState.value === 'saving') return
  const sent = draft.value
  saveState.value = 'saving'
  try {
    const saved = await api.put(
      '/api/planner/today/note',
      { body: sent, note_date: note.value.date, base_updated_at: note.value.updated_at },
      { suppressErrorToast: true },
    )
    note.value = saved
    saveState.value = 'saved'
    if (draft.value !== sent) schedule() // typed while the save was in flight
  } catch (e) {
    const detail = e?.body?.detail
    if (e?.status === 409 && detail?.current) {
      conflict.value = { reason: detail.reason, current: detail.current }
      saveState.value = 'idle'
    } else {
      saveState.value = 'error'
    }
  }
}

function useLatest() {
  if (!conflict.value) return // a double tap
  const c = conflict.value.current
  conflict.value = null
  note.value = c
  draft.value = c.body || ''
  saveState.value = 'idle'
}

function keepMine() {
  if (!conflict.value) return // a double tap
  note.value = conflict.value.current // save on top of the version just seen
  conflict.value = null
  saveNote()
}

watch(draft, () => {
  if (dirty.value && !conflict.value) schedule()
})

async function pin(task, on) {
  await api.put(`/api/planner/tasks/${task.id}/today`, { on })
  await load()
  emit('changed')
}

async function toggleDone(task) {
  const status = task._done ? 'done' : 'todo'
  await api.patch(`/api/planner/tasks/${task.id}`, { status })
  await load()
  emit('changed')
}

async function quickAdd() {
  const title = quickTitle.value.trim()
  if (!title || adding.value) return
  adding.value = true
  try {
    await api.post('/api/planner/tasks', { title, today: true })
    quickTitle.value = ''
    await load()
    emit('changed')
  } finally {
    adding.value = false
  }
}

// Pick up changes made elsewhere — the AI, the quick-capture sheet, another
// tab — without clobbering anything typed here (load() guards the note).
function refresh() { load() }
// Hidden is the last reliable moment on a phone (an app switch): flush a
// pending note instead of waiting out the autosave delay. On a tab CLOSE the
// browser may cancel the in-flight request (plain fetch, no keepalive), so
// that case is best-effort.
function onVisible() {
  if (document.visibilityState === 'visible') load()
  else saveNote()
}

onMounted(() => {
  load()
  window.addEventListener('gdx:planner-refresh', refresh)
  window.addEventListener('focus', refresh)
  document.addEventListener('visibilitychange', onVisible)
})

onUnmounted(() => {
  window.removeEventListener('gdx:planner-refresh', refresh)
  window.removeEventListener('focus', refresh)
  document.removeEventListener('visibilitychange', onVisible)
  saveNote() // leaving mid-thought still saves it
})

defineExpose({ reload: load })
</script>

<style scoped>
.today-panel { display: flex; flex-direction: column; gap: 1rem; }
.today-date { margin: 0; font-size: 1.05rem; }
.w-full { width: 100%; }
.flex-1 { flex: 1; min-width: 0; }
.section-row { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem; margin-bottom: 0.35rem; }
.section-label { margin: 0; font-weight: 600; font-size: 0.9rem; }
.note-status { font-size: 0.78rem; color: var(--p-text-muted-color); }

.note-conflict { margin-top: 0.5rem; padding: 0.6rem 0.75rem; border: 1px solid var(--p-orange-400, #f59e0b); border-radius: 8px; background: var(--p-content-hover-background); }
.conflict-lead { margin: 0 0 0.5rem; font-size: 0.88rem; }
.conflict-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.conflict-label { font-size: 0.78rem; font-weight: 600; color: var(--p-text-muted-color); margin-bottom: 0.25rem; }
.conflict-text { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: inherit; font-size: 0.85rem; max-height: 10rem; overflow: auto; padding: 0.4rem; border: 1px solid var(--p-content-border-color); border-radius: 6px; background: var(--p-content-background); }
.conflict-actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 0.5rem; }

.today-add { display: flex; gap: 0.5rem; }
.today-empty { padding: 1rem; text-align: center; color: var(--p-text-muted-color); border: 1px dashed var(--p-content-border-color); border-radius: 8px; }
.today-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.4rem; }
.today-row { display: flex; align-items: center; gap: 0.6rem; padding: 0.45rem 0.6rem; background: var(--p-content-hover-background); border: 1px solid var(--p-content-border-color); border-radius: 8px; }
.today-row.urgent { border-left: 3px solid #ef4444; }
.today-row.high { border-left: 3px solid #f59e0b; }
.today-row.suggest { background: transparent; }
.row-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.2rem; text-align: left; background: none; border: 0; padding: 0.15rem 0; color: inherit; font: inherit; cursor: pointer; }
.row-title { font-weight: 600; overflow-wrap: anywhere; }
.row-title.done { text-decoration: line-through; opacity: 0.55; }
.row-meta { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; }
.meta-item { font-size: 0.78rem; color: var(--p-text-muted-color); display: inline-flex; align-items: center; gap: 0.2rem; }
.meta-item.overdue { color: var(--p-red-500, #ef4444); }
.ai-tag { font-size: 0.68rem; padding: 0 0.35rem; }
.pill { font-size: 0.68rem; font-weight: 700; padding: 0 0.35rem; border-radius: 4px; }
.pill-danger { background: #ef4444; color: #fff; }
.pill-warn { background: #f59e0b; color: #111; }
.suggest-group + .suggest-group { margin-top: 0.75rem; }
.suggest-title { font-size: 0.8rem; font-weight: 600; color: var(--p-text-muted-color); margin: 0.5rem 0 0.35rem; }
.more-link { margin-top: 0.5rem; background: none; border: 0; padding: 0; color: var(--p-primary-color); cursor: pointer; font: inherit; font-size: 0.85rem; }

/* Phone: thumb-sized targets (44px), single column conflict view. */
.compact .row-action, .compact .today-add :deep(.p-button) { min-width: 44px; min-height: 44px; }
.compact .today-row { min-height: 52px; }
.compact .conflict-cols { grid-template-columns: 1fr; }
.compact .more-link { min-height: 44px; }
</style>
