<template>
    <section class="mobile-planner">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Planner</h1>
          <button
            type="button"
            class="head-add"
            :aria-label="addLabel"
            @click="openCreate"
            data-test="mp-head-add"
          >
            <i class="pi pi-plus" /> New
          </button>
        </div>
        <SelectButton
          v-model="activeTab"
          :options="TABS"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          aria-label="Planner section"
          class="tab-switch"
        />
      </header>

      <!-- TASKS -->
      <template v-if="activeTab === 'tasks'">
        <div class="filter-row">
          <SelectButton
            v-model="taskView"
            :options="TASK_VIEWS"
            optionLabel="label"
            optionValue="value"
            :allowEmpty="false"
            aria-label="Task view"
          />
          <Select
            v-model="taskSort"
            :options="TASK_SORTS"
            optionLabel="label"
            optionValue="value"
            aria-label="Sort tasks"
            class="sort-select"
            data-test="mp-task-sort"
          />
        </div>

        <div v-if="tasksLoading && !tasks.length" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
          <span>Loading tasks…</span>
        </div>
        <div v-else-if="!tasks.length" class="state-msg">
          <i class="pi pi-check-square empty-icon" />
          <div class="empty-title">No tasks</div>
          <div class="empty-help">Tap + to add one.</div>
        </div>
        <ol v-else class="card-list">
          <li
            v-for="task in tasks"
            :key="task.id"
            :class="['task-card', task.priority, task.status]"
            data-test="mp-task-card"
          >
            <Checkbox
              v-model="task._done"
              :binary="true"
              @change="toggleTask(task)"
              aria-label="Mark task done"
            />
            <button type="button" class="task-body" @click="editTask(task)">
              <div class="task-title" :class="{ done: task.status === 'done' }">
                {{ task.title }}
              </div>
              <div class="task-meta">
                <span v-if="task.priority === 'urgent'" class="pill pill-danger">URGENT</span>
                <span v-else-if="task.priority === 'high'" class="pill pill-warn">HIGH</span>
                <span v-if="task.due_date" class="meta-item">
                  <i class="pi pi-calendar" />
                  {{ shortDate(task.due_date) }}
                </span>
                <span v-if="task.assignee_name" class="meta-item">
                  <i class="pi pi-user" />
                  {{ task.assignee_name }}
                </span>
                <span v-if="jobLabelFor(task.job_id)" class="meta-item">
                  <i class="pi pi-briefcase" />
                  {{ jobLabelFor(task.job_id) }}
                </span>
              </div>
            </button>
          </li>
        </ol>
      </template>

      <!-- PLANS -->
      <template v-else-if="activeTab === 'plans'">
        <div v-if="plansLoading && !plans.length" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
          <span>Loading plans…</span>
        </div>
        <div v-else-if="!plans.length" class="state-msg">
          <i class="pi pi-list empty-icon" />
          <div class="empty-title">No plans yet</div>
          <div class="empty-help">Tap + to create one.</div>
        </div>
        <ol v-else class="card-list">
          <li
            v-for="plan in plans"
            :key="plan.id"
            class="plan-card"
            data-test="mp-plan-card"
          >
            <button type="button" class="plan-button" @click="openPlan(plan)">
              <div class="plan-header">
                <strong>{{ plan.title }}</strong>
                <span v-if="plan.is_template" class="pill pill-info">Template</span>
              </div>
              <div class="plan-progress">
                <div class="progress-bar">
                  <div class="progress-fill" :style="{ width: (plan.progress || 0) + '%' }" />
                </div>
                <span class="progress-text">{{ plan.done_steps || 0 }}/{{ plan.total_steps || 0 }}</span>
              </div>
            </button>
          </li>
        </ol>
      </template>

      <!-- MESSAGES -->
      <template v-else-if="activeTab === 'messages'">
        <div v-if="threadsLoading && !threads.length" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
          <span>Loading conversations…</span>
        </div>
        <div v-else-if="!threads.length" class="state-msg">
          <i class="pi pi-comments empty-icon" />
          <div class="empty-title">No conversations</div>
          <div class="empty-help">Tap + to start one.</div>
        </div>
        <ol v-else class="card-list">
          <li
            v-for="thread in threads"
            :key="thread.id"
            class="thread-card"
            data-test="mp-thread-card"
          >
            <button type="button" class="thread-button" @click="openThread(thread)">
              <div class="thread-row">
                <div class="thread-name">
                  <i :class="thread.type === 'group' ? 'pi pi-users' : 'pi pi-user'" />
                  {{ thread.name || 'Direct Message' }}
                </div>
                <span v-if="thread.unread > 0" class="pill pill-danger">
                  {{ thread.unread }}
                </span>
              </div>
              <div class="thread-preview">{{ thread.last_message || 'No messages yet' }}</div>
            </button>
          </li>
        </ol>
      </template>

      <!-- Task create -->
      <Dialog
        v-model:visible="showTaskForm"
        header="New Task"
        modal
        :style="{ width: '95vw', maxWidth: '500px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div class="form-stack">
          <InputText v-model="taskForm.title" placeholder="Task title" class="w-full" />
          <Textarea v-model="taskForm.description" placeholder="Description (optional)" rows="2" autoResize class="w-full" />
          <Select v-model="taskForm.priority" :options="['low','medium','high','urgent']" placeholder="Priority" class="w-full" />
          <DatePicker v-model="taskForm.due_date" dateFormat="yy-mm-dd" placeholder="Due date" :showIcon="true" :showClear="true" class="w-full" />
          <Select v-model="taskForm.assigned_to" :options="userOptions" optionLabel="label" optionValue="value" placeholder="Assign to" :showClear="true" filter class="w-full" />
          <Select v-model="taskForm.job_id" :options="jobOptions" optionLabel="label" optionValue="value" placeholder="Linked job" :showClear="true" filter class="w-full" />
          <Select v-model="taskForm.customer_id" :options="customerOptions" optionLabel="label" optionValue="value" placeholder="Linked customer" :showClear="true" filter class="w-full" />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="showTaskForm = false" />
          <Button label="Create" :loading="taskSaving" @click="createTask" />
        </template>
      </Dialog>

      <!-- Task detail -->
      <Dialog
        v-model:visible="showTaskDetail"
        header="Task"
        modal
        :style="{ width: '95vw', maxWidth: '520px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div v-if="selectedTask" class="form-stack">
          <InputText v-model="selectedTask.title" placeholder="Task title" class="w-full" />
          <Textarea v-model="selectedTask.description" placeholder="Description" rows="4" autoResize class="w-full" />
          <Select v-model="selectedTask.priority" :options="['low','medium','high','urgent']" placeholder="Priority" class="w-full" />
          <Select v-model="selectedTask.status" :options="['todo','in-progress','done']" placeholder="Status" class="w-full" />
          <DatePicker v-model="selectedTask.due_date" dateFormat="yy-mm-dd" placeholder="Due date" :showIcon="true" :showClear="true" class="w-full" />
          <Select v-model="selectedTask.assigned_to" :options="userOptions" optionLabel="label" optionValue="value" placeholder="Assign to" :showClear="true" filter class="w-full" />
          <Select v-model="selectedTask.job_id" :options="jobOptions" optionLabel="label" optionValue="value" placeholder="Linked job" :showClear="true" filter class="w-full" />
          <Select v-model="selectedTask.customer_id" :options="customerOptions" optionLabel="label" optionValue="value" placeholder="Linked customer" :showClear="true" filter class="w-full" />

          <!-- Captured call with a number but no customer → offer to create one. -->
          <div v-if="selectedTask.contact_phone && !selectedTask.customer_id" class="capture-cta">
            <p class="capture-cta-text">
              <i class="pi pi-phone" aria-hidden="true" />
              Captured from a call — {{ selectedTask.contact_phone }} isn't linked to a customer yet.
            </p>
            <Button
              label="Create customer from this call"
              icon="pi pi-user-plus"
              size="small"
              outlined
              class="w-full"
              @click="createCustomerFromTask"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="showTaskDetail = false" />
          <Button label="Save" :loading="taskEditSaving" @click="saveTaskEdits" />
        </template>
      </Dialog>

      <CustomerFormDialog
        v-model:visible="showCustomerCreate"
        mode="create"
        :customer="customerPrefill"
        @saved="onCustomerCreatedFromTask"
      />

      <!-- Plan create -->
      <Dialog
        v-model:visible="showPlanForm"
        header="New Plan"
        modal
        :style="{ width: '95vw', maxWidth: '500px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div class="form-stack">
          <InputText v-model="planForm.title" placeholder="Plan title" class="w-full" />
          <Textarea v-model="planForm.description" placeholder="Description" rows="2" autoResize class="w-full" />
          <label class="checkbox-row">
            <Checkbox v-model="planForm.is_template" :binary="true" />
            <span>Save as template</span>
          </label>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="showPlanForm = false" />
          <Button label="Create" :loading="planSaving" @click="createPlan" />
        </template>
      </Dialog>

      <!-- Thread create -->
      <Dialog
        v-model:visible="showThreadForm"
        header="New Conversation"
        modal
        :style="{ width: '95vw', maxWidth: '440px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div class="form-stack">
          <InputText v-model="threadForm.name" placeholder="Thread name (groups)" class="w-full" />
          <Select v-model="threadForm.type" :options="['direct','group']" placeholder="Type" class="w-full" />
          <Select v-model="threadForm.members" :options="userOptions" optionLabel="label" optionValue="value" placeholder="Add members" filter :multiple="true" class="w-full" />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="showThreadForm = false" />
          <Button label="Start" @click="createThread" />
        </template>
      </Dialog>

      <!-- Messages drawer (full-screen on phone) -->
      <Dialog
        v-model:visible="showMessages"
        :header="activeThread?.name || 'Messages'"
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
        class="msg-dialog"
      >
        <div class="messages-body">
          <div
            v-for="msg in threadMessages"
            :key="msg.id"
            class="msg"
            :class="{ mine: msg.sender_id === myId }"
          >
            <div class="msg-sender">{{ msg.sender_name }}</div>
            <div class="msg-text">{{ msg.body }}</div>
            <div class="msg-time">{{ shortTime(msg.created_at) }}</div>
          </div>
        </div>
        <template #footer>
          <div class="msg-input-row">
            <InputText
              v-model="newMessage"
              placeholder="Type a message…"
              class="flex-1"
              @keyup.enter="sendMessage"
            />
            <Button v-tooltip="'Send'" icon="pi pi-send" aria-label="Send" :disabled="!newMessage.trim()" @click="sendMessage" />
          </div>
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useApiWithToast } from '../composables/useApiWithToast'
import CustomerFormDialog from '../components/CustomerFormDialog.vue'

import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import DatePicker from 'primevue/datepicker'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Textarea from 'primevue/textarea'

const api = useApiWithToast()

const TABS = [
  { label: 'Tasks', value: 'tasks' },
  { label: 'Plans', value: 'plans' },
  { label: 'Chat', value: 'messages' },
]
const TASK_VIEWS = [
  { label: 'Mine', value: 'mine' },
  { label: 'Delegated', value: 'delegated' },
  { label: 'All', value: 'all' },
  { label: 'Completed', value: 'completed' },
]
const TASK_SORTS = [
  { label: 'Needs action', value: 'needs_action' },
  { label: 'Newest', value: 'newest' },
  { label: 'Oldest', value: 'oldest' },
  { label: 'Priority', value: 'priority' },
  { label: 'Due date', value: 'due_date' },
]

const activeTab = ref('tasks')
const myId = ref(sessionStorage.getItem('gdx_user_id') || '')

// Tasks
const tasks = ref([])
const tasksLoading = ref(true)
const taskView = ref('mine')
// Default to "needs action" so overdue + today (and every fresh call-capture,
// which is due today) sit at the top instead of scrolling away.
const taskSort = ref('needs_action')
const showTaskForm = ref(false)
const taskSaving = ref(false)
const taskForm = ref(emptyTaskForm())
const showTaskDetail = ref(false)
const selectedTask = ref(null)
const taskEditSaving = ref(false)

// Plans
const plans = ref([])
const plansLoading = ref(false)
const showPlanForm = ref(false)
const planSaving = ref(false)
const planForm = ref({ title: '', description: '', is_template: false })

// Messages
const threads = ref([])
const threadsLoading = ref(false)
const showThreadForm = ref(false)
const threadForm = ref({ name: '', type: 'direct', members: [] })
const showMessages = ref(false)
const activeThread = ref(null)
const threadMessages = ref([])
const newMessage = ref('')

// Lookups
const userOptions = ref([])
const jobOptions = ref([])
const customerOptions = ref([])

const addLabel = computed(() => {
  if (activeTab.value === 'plans') return 'New plan'
  if (activeTab.value === 'messages') return 'New conversation'
  return 'New task'
})

function emptyTaskForm() {
  return {
    title: '',
    description: '',
    priority: 'low',
    due_date: null,
    assigned_to: null,
    job_id: null,
    customer_id: null,
  }
}

function shortDate(d) {
  if (!d) return ''
  try {
    return new Date(d).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  } catch {
    return d
  }
}

function shortTime(d) {
  if (!d) return ''
  try {
    return new Date(d).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  } catch {
    return d
  }
}

function jobLabelFor(jobId) {
  if (!jobId) return ''
  const hit = jobOptions.value.find((o) => o.value === jobId)
  return hit ? hit.label : ''
}

async function loadTasks() {
  tasksLoading.value = true
  try {
    const isCompleted = taskView.value === 'completed'
    const view = isCompleted ? 'all' : taskView.value
    const bucket = isCompleted ? 'completed' : 'active'
    const data = await api.get(
      `/api/planner/tasks?view=${view}&bucket=${bucket}&sort=${taskSort.value}`,
    )
    tasks.value = (data.items || []).map((t) => ({ ...t, _done: t.status === 'done' }))
  } catch {
    tasks.value = []
  } finally {
    tasksLoading.value = false
  }
}

async function loadPlans() {
  plansLoading.value = true
  try {
    const data = await api.get('/api/planner/plans')
    plans.value = data.items || []
  } catch {
    plans.value = []
  } finally {
    plansLoading.value = false
  }
}

async function loadThreads() {
  threadsLoading.value = true
  try {
    const data = await api.get('/api/planner/threads')
    threads.value = data.items || []
  } catch {
    threads.value = []
  } finally {
    threadsLoading.value = false
  }
}

async function loadUsers() {
  try {
    const data = await api.get('/api/users')
    const list = Array.isArray(data) ? data : data?.items || []
    userOptions.value = list.map((u) => ({ label: u.username || u.email || u.id, value: u.id }))
  } catch {
    userOptions.value = []
  }
}

async function loadJobsAndCustomers() {
  try {
    const data = await api.get('/api/jobs')
    const list = Array.isArray(data) ? data : data?.items || []
    jobOptions.value = list.map((j) => ({ label: j.title || j.id, value: j.id }))
  } catch {
    jobOptions.value = []
  }
  try {
    const data = await api.get('/api/customers')
    const list = Array.isArray(data) ? data : data?.items || []
    customerOptions.value = list.map((c) => ({ label: c.name || c.email || c.id, value: c.id }))
  } catch {
    customerOptions.value = []
  }
}

function openCreate() {
  if (activeTab.value === 'plans') {
    showPlanForm.value = true
    return
  }
  if (activeTab.value === 'messages') {
    showThreadForm.value = true
    return
  }
  taskForm.value = emptyTaskForm()
  showTaskForm.value = true
}

async function createTask() {
  taskSaving.value = true
  try {
    const due = taskForm.value.due_date instanceof Date
      ? taskForm.value.due_date.toISOString().slice(0, 10)
      : taskForm.value.due_date
    await api.post('/api/planner/tasks', { ...taskForm.value, due_date: due }, { successMessage: 'Task created' })
    showTaskForm.value = false
    taskForm.value = emptyTaskForm()
    await loadTasks()
  } finally {
    taskSaving.value = false
  }
}

async function toggleTask(task) {
  const newStatus = task._done ? 'done' : 'todo'
  await api.patch(`/api/planner/tasks/${task.id}`, { status: newStatus })
  task.status = newStatus
  // Done items disappear from active buckets and appear in the Completed tab;
  // reload so the row leaves/enters the visible list immediately.
  await loadTasks()
}

function editTask(task) {
  selectedTask.value = {
    id: task.id,
    title: task.title || '',
    description: task.description || '',
    priority: task.priority || 'low',
    status: task.status || 'todo',
    due_date: task.due_date || null,
    assigned_to: task.assigned_to || null,
    job_id: task.job_id || null,
    customer_id: task.customer_id || null,
    contact_phone: task.contact_phone || null,
    phone_com_call_id: task.phone_com_call_id || null,
    source: task.source || null,
  }
  showTaskDetail.value = true
}

// ── Create-customer-from-capture (2026-07-07) ──
// A captured call note may carry a phone number but no customer. Offer to spin
// up the customer from the task; on save, link it back and backfill the call
// rows so the cold-leads queue shrinks.
const showCustomerCreate = ref(false)
const customerPrefill = ref(null)

function createCustomerFromTask() {
  if (!selectedTask.value?.contact_phone) return
  customerPrefill.value = { phone: selectedTask.value.contact_phone }
  showCustomerCreate.value = true
}

async function onCustomerCreatedFromTask(saved) {
  const newId = saved?.id
  const task = selectedTask.value
  if (!newId || !task?.id) return
  try {
    await api.post(
      `/api/planner/tasks/${task.id}/link-customer`,
      { customer_id: newId },
      { successMessage: 'Customer created and linked' },
    )
    task.customer_id = newId
    await loadJobsAndCustomers()
    await loadTasks()
  } catch { /* toast handled */ }
}

async function saveTaskEdits() {
  if (!selectedTask.value) return
  taskEditSaving.value = true
  try {
    const t = selectedTask.value
    await api.patch(`/api/planner/tasks/${t.id}`, {
      title: t.title,
      description: t.description,
      priority: t.priority,
      status: t.status,
      due_date: t.due_date,
      assigned_to: t.assigned_to,
      job_id: t.job_id,
      customer_id: t.customer_id,
    }, { successMessage: 'Task updated' })
    showTaskDetail.value = false
    selectedTask.value = null
    await loadTasks()
  } finally {
    taskEditSaving.value = false
  }
}

async function createPlan() {
  planSaving.value = true
  try {
    await api.post('/api/planner/plans', planForm.value, { successMessage: 'Plan created' })
    showPlanForm.value = false
    planForm.value = { title: '', description: '', is_template: false }
    await loadPlans()
  } finally {
    planSaving.value = false
  }
}

function openPlan(_plan) {
  // Plan detail not yet shipped on either desktop or mobile. Keep parity.
}

async function createThread() {
  try {
    await api.post('/api/planner/threads', threadForm.value, { successMessage: 'Conversation started' })
    showThreadForm.value = false
    threadForm.value = { name: '', type: 'direct', members: [] }
    await loadThreads()
  } catch { /* toast handled */ }
}

async function openThread(thread) {
  activeThread.value = thread
  showMessages.value = true
  try {
    const data = await api.get(`/api/planner/threads/${thread.id}/messages`)
    threadMessages.value = data.items || []
    thread.unread = 0
  } catch {
    threadMessages.value = []
  }
}

async function sendMessage() {
  if (!newMessage.value.trim() || !activeThread.value) return
  try {
    await api.post(`/api/planner/threads/${activeThread.value.id}/messages`, { body: newMessage.value })
    newMessage.value = ''
    const data = await api.get(`/api/planner/threads/${activeThread.value.id}/messages`)
    threadMessages.value = data.items || []
  } catch { /* toast handled */ }
}

watch([taskView, taskSort], loadTasks)
watch(activeTab, (tab) => {
  if (tab === 'plans' && plans.value.length === 0) loadPlans()
  if (tab === 'messages' && threads.value.length === 0) loadThreads()
})

// A quick-capture from the FAB fires this on the window; reload so the new
// note appears without a manual refresh.
function onExternalCapture() {
  if (activeTab.value === 'tasks') loadTasks()
}

onMounted(() => {
  loadTasks()
  loadUsers()
  loadJobsAndCustomers()
  window.addEventListener('gdx:planner-refresh', onExternalCapture)
})

onUnmounted(() => {
  window.removeEventListener('gdx:planner-refresh', onExternalCapture)
})
</script>

<style scoped>
.mobile-planner {
  /* Bottom padding clears the fixed AppBottomNav (var(--bottom-nav-height,
   * 64px)) plus iOS home indicator. Was a flat 5rem; now derives from the
   * canonical nav-height variable so it tracks design tokens. */
  padding: 0.75rem 0.75rem calc(var(--bottom-nav-height, 64px) + 1.25rem + env(safe-area-inset-bottom));
  max-width: 800px;
  margin: 0 auto;
  position: relative;
}

/* Captured-call CTA in the task detail dialog. */
.capture-cta {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px dashed var(--border-subtle);
  border-radius: 0.625rem;
  background: var(--surface-elevated);
}
.capture-cta-text {
  margin: 0;
  font-size: 0.85rem;
  color: var(--text-muted);
}

.mobile-page-head {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}

.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.head-add {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.45rem 0.8rem;
  border-radius: 0.5rem;
  background: var(--p-primary-color, #2563eb);
  color: #fff;
  border: 0;
  font-weight: 600;
  font-size: 0.9rem;
  cursor: pointer;
}

.head-add:active {
  transform: scale(0.97);
}

.head-add i {
  font-size: 0.85rem;
}

.tab-switch :deep(.p-selectbutton) {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  width: 100%;
}

.tab-switch :deep(.p-selectbutton .p-button) {
  padding-block: 0.5rem;
}

.filter-row {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.filter-row :deep(.p-selectbutton) {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  width: 100%;
}

.filter-row :deep(.p-selectbutton .p-button) {
  padding-block: 0.5rem;
  /* Tight font on phones so "Delegated" / "Completed" don't truncate */
  font-size: 0.82rem;
}

.sort-select {
  width: 100%;
}

.sort-select :deep(.p-select-label) {
  padding-block: 0.5rem;
}

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

/* TASKS */
.task-card {
  display: flex;
  align-items: flex-start;
  gap: 0.6rem;
  padding: 0.75rem;
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem;
}

/* Tap-target compliance — Apple HIG (44×44 pt). The PrimeVue Checkbox
 * renders a 20×20 box; on mobile every "Mark task done" tap landed on
 * a 20px target (less than half the minimum). Wrap the visual box in
 * a 44×44 invisible hit area centered on the box so the visible
 * affordance stays small but the tap target meets the standard.
 * Caught S112 2026-05-09 mobile audit. */
.task-card .p-checkbox {
  min-width: 44px;
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.task-card .p-checkbox-input,
.task-card .p-checkbox-box {
  width: 24px;
  height: 24px;
}

.task-card.urgent {
  border-left: 3px solid #ef4444;
}

.task-card.high {
  border-left: 3px solid #f59e0b;
}

.task-body {
  flex: 1;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 0;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.task-title {
  font-weight: 600;
  font-size: 1rem;
  line-height: 1.3;
}

.task-title.done {
  text-decoration: line-through;
  opacity: 0.5;
}

.task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
  margin-top: 0.35rem;
}

.meta-item {
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
}

/* PLANS */
.plan-card,
.thread-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem;
}

.plan-button,
.thread-button {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 0.75rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.plan-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
  gap: 0.5rem;
}

.plan-progress {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.progress-bar {
  flex: 1;
  height: 6px;
  background: var(--p-content-border-color, #e5e7eb);
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--p-primary-color, #2563eb);
  border-radius: 3px;
  transition: width 0.3s;
}

.progress-text {
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 600;
}

/* THREADS */
.thread-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.25rem;
}

.thread-name {
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}

.thread-preview {
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* PILLS */
.pill {
  display: inline-flex;
  align-items: center;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.pill-danger {
  background: #ef4444;
  color: #fff;
}

.pill-warn {
  background: #f59e0b;
  color: #1f2937;
}

.pill-info {
  background: #2563eb;
  color: #fff;
}

/* STATES */
.state-msg {
  text-align: center;
  padding: 2.5rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
}

.empty-icon {
  font-size: 2rem;
  opacity: 0.5;
}

.empty-title {
  font-size: 1.05rem;
  font-weight: 600;
}

.empty-help {
  font-size: 0.85rem;
}

/* FORMS */
.w-full { width: 100%; }
.flex-1 { flex: 1; }

.form-stack {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
}

.checkbox-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

/* MESSAGES DIALOG */
.messages-body {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.5rem;
  min-height: 50vh;
}

.msg {
  max-width: 85%;
  padding: 0.5rem 0.75rem;
  border-radius: 12px;
  background: var(--p-content-hover-background, #f3f4f6);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
}

.msg.mine {
  align-self: flex-end;
  background: var(--p-primary-color, #2563eb);
  color: #fff;
  border-color: transparent;
}

.msg-sender {
  font-size: 0.7rem;
  font-weight: 700;
  opacity: 0.7;
}

.msg-text {
  font-size: 0.95rem;
}

.msg-time {
  font-size: 0.65rem;
  opacity: 0.6;
  text-align: right;
}

.msg-input-row {
  display: flex;
  gap: 0.5rem;
  width: 100%;
  padding-bottom: env(safe-area-inset-bottom);
}
</style>
