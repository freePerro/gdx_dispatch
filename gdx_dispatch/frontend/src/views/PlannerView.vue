<template>
    <section class="planner-view view-card">
      <Toolbar>
        <template #start><h2 class="page-title">Planner</h2></template>
        <template #end>
          <Button v-if="activeTab === 'tasks'" label="+ Task" icon="pi pi-plus" size="small" @click="showTaskForm = true" data-testid="new-task" />
          <Button v-if="activeTab === 'plans'" label="+ Plan" icon="pi pi-plus" size="small" @click="showPlanForm = true" data-testid="new-plan" />
          <Button v-if="activeTab === 'messages'" label="+ Thread" icon="pi pi-plus" size="small" @click="showThreadForm = true" data-testid="new-thread" />
        </template>
      </Toolbar>

      <Tabs v-model:value="activeTab">
        <TabList>
          <Tab value="tasks">My Tasks</Tab>
          <Tab value="plans">Plans</Tab>
          <Tab value="messages">Messages <Badge v-if="totalUnread" :value="totalUnread" severity="danger" /></Tab>
        </TabList>
      </Tabs>

      <!-- TASKS TAB -->
      <div v-if="activeTab === 'tasks'" class="tab-content">
        <div class="task-filters">
          <SelectButton v-model="taskView" :options="TASK_VIEWS"
            optionLabel="label" optionValue="value" :allowEmpty="false" size="small" />
          <Select v-model="taskSort" :options="TASK_SORTS" optionLabel="label" optionValue="value"
            size="small" class="sort-select" data-testid="task-sort" />
        </div>
        <div v-if="tasksLoading" class="loading"><ProgressSpinner /></div>
        <div v-else-if="!tasks.length" class="empty">No tasks. Create one to get started.</div>
        <div v-else class="task-list">
          <div v-for="task in tasks" :key="task.id" class="task-card" :class="[task.priority, task.status]">
            <Checkbox v-model="task._done" :binary="true" @change="toggleTask(task)" />
            <div class="task-body" @click="editTask(task)">
              <div class="task-title" :class="{done: task.status === 'done'}">{{ task.title }}</div>
              <div class="task-meta">
                <Tag v-if="task.priority === 'urgent'" value="URGENT" severity="danger" size="small" />
                <Tag v-else-if="task.priority === 'high'" value="HIGH" severity="warn" size="small" />
                <span v-if="task.assignee_name" class="meta-item"><i class="pi pi-user"></i> {{ task.assignee_name }}</span>
                <span v-if="task.due_date" class="meta-item"><i class="pi pi-calendar"></i> {{ shortDate(task.due_date) }}</span>
                <span v-if="jobLabelFor(task.job_id)" class="meta-item"><i class="pi pi-briefcase"></i> {{ jobLabelFor(task.job_id) }}</span>
                <span v-if="customerLabelFor(task.customer_id)" class="meta-item"><i class="pi pi-user-edit"></i> {{ customerLabelFor(task.customer_id) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- PLANS TAB -->
      <div v-if="activeTab === 'plans'" class="tab-content">
        <div v-if="plansLoading" class="loading"><ProgressSpinner /></div>
        <div v-else-if="!plans.length" class="empty">No plans yet. Create one or use a template.</div>
        <div v-else class="plan-list">
          <div v-for="plan in plans" :key="plan.id" class="plan-card">
            <div class="plan-header">
              <strong>{{ plan.title }}</strong>
              <Tag v-if="plan.is_template" value="Template" severity="info" size="small" />
            </div>
            <div class="plan-progress">
              <div class="progress-bar"><div class="progress-fill" :style="{width: plan.progress + '%'}"></div></div>
              <span class="progress-text">{{ plan.done_steps }}/{{ plan.total_steps }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- MESSAGES TAB -->
      <div v-if="activeTab === 'messages'" class="tab-content">
        <div v-if="threadsLoading" class="loading"><ProgressSpinner /></div>
        <div v-else-if="!threads.length" class="empty">No conversations. Start one with a teammate.</div>
        <div v-else class="thread-list">
          <div v-for="thread in threads" :key="thread.id" class="thread-card" @click="openThread(thread)">
            <div class="thread-name">
              <i :class="thread.type === 'group' ? 'pi pi-users' : 'pi pi-user'"></i>
              {{ thread.name || 'Direct Message' }}
            </div>
            <div class="thread-preview">{{ thread.last_message || 'No messages yet' }}</div>
            <Badge v-if="thread.unread > 0" :value="thread.unread" severity="danger" class="unread-badge" />
          </div>
        </div>
      </div>

      <!-- Task Create Dialog -->
      <Dialog v-model:visible="showTaskForm" header="New Task" modal :style="{width:'500px'}">
        <div class="form-stack">
          <InputText v-model="taskForm.title" placeholder="Task title" class="w-full" />
          <Textarea v-model="taskForm.description" placeholder="Description (optional)" rows="2" class="w-full" />
          <div class="form-row">
            <Select v-model="taskForm.priority" :options="['low','medium','high','urgent']" placeholder="Priority" class="flex-1" />
            <DatePicker v-model="taskForm.due_date" dateFormat="yy-mm-dd" placeholder="Due date" :showIcon="true" class="flex-1" />
          </div>
          <Select v-model="taskForm.assigned_to" :options="userOptions" optionLabel="label" optionValue="value"
            placeholder="Assign to (optional)" :showClear="true" filter class="w-full" />
          <!-- Link to a job or customer. Either/neither/both. -->
          <Select v-model="taskForm.job_id" :options="jobOptions" optionLabel="label" optionValue="value"
            placeholder="Linked job (optional)" :showClear="true" filter class="w-full" data-testid="task-job" />
          <Select v-model="taskForm.customer_id" :options="customerOptions" optionLabel="label" optionValue="value"
            placeholder="Linked customer (optional)" :showClear="true" filter class="w-full" data-testid="task-customer" />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showTaskForm = false" />
          <Button label="Create Task" @click="createTask" :loading="taskSaving" />
        </template>
      </Dialog>

      <!-- Task Detail / Edit Dialog -->
      <Dialog v-model:visible="showTaskDetail" header="Task" modal :style="{width:'520px'}" data-testid="task-detail">
        <div v-if="selectedTask" class="form-stack">
          <InputText v-model="selectedTask.title" placeholder="Task title" class="w-full" data-testid="task-detail-title" />
          <Textarea v-model="selectedTask.description" placeholder="Description" rows="5" class="w-full" data-testid="task-detail-description" autoResize />
          <div class="form-row">
            <Select v-model="selectedTask.priority" :options="['low','medium','high','urgent']" placeholder="Priority" class="flex-1" />
            <Select v-model="selectedTask.status" :options="['todo','in-progress','done']" placeholder="Status" class="flex-1" />
          </div>
          <DatePicker v-model="selectedTask.due_date" dateFormat="yy-mm-dd" placeholder="Due date" :showIcon="true" :showClear="true" class="w-full" />
          <Select v-model="selectedTask.assigned_to" :options="userOptions" optionLabel="label" optionValue="value"
            placeholder="Assign to" :showClear="true" filter class="w-full" />
          <Select v-model="selectedTask.job_id" :options="jobOptions" optionLabel="label" optionValue="value"
            placeholder="Linked job" :showClear="true" filter class="w-full" />
          <Select v-model="selectedTask.customer_id" :options="customerOptions" optionLabel="label" optionValue="value"
            placeholder="Linked customer" :showClear="true" filter class="w-full" />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showTaskDetail = false" />
          <Button label="Save" @click="saveTaskEdits" :loading="taskEditSaving" data-testid="task-detail-save" />
        </template>
      </Dialog>

      <!-- Plan Create Dialog -->
      <Dialog v-model:visible="showPlanForm" header="New Plan" modal :style="{width:'500px'}">
        <div class="form-stack">
          <InputText v-model="planForm.title" placeholder="Plan title" class="w-full" />
          <Textarea v-model="planForm.description" placeholder="Description" rows="2" class="w-full" />
          <div class="form-row"><Checkbox v-model="planForm.is_template" :binary="true" /><label>Save as template</label></div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showPlanForm = false" />
          <Button label="Create Plan" @click="createPlan" :loading="planSaving" />
        </template>
      </Dialog>

      <!-- Thread Create Dialog -->
      <Dialog v-model:visible="showThreadForm" header="New Conversation" modal :style="{width:'400px'}">
        <div class="form-stack">
          <InputText v-model="threadForm.name" placeholder="Thread name (optional for groups)" class="w-full" />
          <Select v-model="threadForm.type" :options="['direct','group']" placeholder="Type" class="w-full" />
          <Select v-model="threadForm.members" :options="userOptions" optionLabel="label" optionValue="value"
            placeholder="Add members" filter :multiple="true" class="w-full" />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showThreadForm = false" />
          <Button label="Start Conversation" @click="createThread" />
        </template>
      </Dialog>

      <!-- Message Drawer -->
      <Dialog v-model:visible="showMessages" :header="activeThread?.name || 'Messages'" modal :style="{width:'500px',height:'70vh'}" position="right">
        <div class="messages-body">
          <div v-for="msg in threadMessages" :key="msg.id" class="msg" :class="{mine: msg.sender_id === myId}">
            <div class="msg-sender">{{ msg.sender_name }}</div>
            <div class="msg-text">{{ msg.body }}</div>
            <div class="msg-time">{{ shortTime(msg.created_at) }}</div>
          </div>
        </div>
        <template #footer>
          <div class="msg-input-row">
            <InputText v-model="newMessage" placeholder="Type a message..." class="flex-1" @keyup.enter="sendMessage" />
            <Button icon="pi pi-send" @click="sendMessage" :disabled="!newMessage.trim()" />
          </div>
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Badge from "primevue/badge";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import DatePicker from "primevue/datepicker";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import SelectButton from "primevue/selectbutton";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import Tabs from "primevue/tabs";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();
const activeTab = ref("tasks");
const myId = ref(sessionStorage.getItem("gdx_user_id") || "");

const TASK_VIEWS = [
  { label: "Mine", value: "mine" },
  { label: "Delegated", value: "delegated" },
  { label: "All", value: "all" },
  { label: "Completed", value: "completed" },
];
const TASK_SORTS = [
  { label: "Newest", value: "newest" },
  { label: "Oldest", value: "oldest" },
  { label: "Priority", value: "priority" },
  { label: "Due date", value: "due_date" },
];

// Tasks
const tasks = ref([]);
const tasksLoading = ref(true);
const taskView = ref("mine");
const taskSort = ref("newest");
const showTaskForm = ref(false);
const taskSaving = ref(false);
const taskForm = ref({
  title: "",
  description: "",
  priority: "low",  // Doug's default 2026-04-13 — high/urgent require active choice
  due_date: null,
  assigned_to: null,
  job_id: null,
  customer_id: null,
});
// Task detail / edit dialog (opened from clicking a task card).
const showTaskDetail = ref(false);
const selectedTask = ref(null);
const taskEditSaving = ref(false);

// Plans
const plans = ref([]);
const plansLoading = ref(true);
const showPlanForm = ref(false);
const planSaving = ref(false);
const planForm = ref({ title: "", description: "", is_template: false });

// Messages
const threads = ref([]);
const threadsLoading = ref(true);
const showThreadForm = ref(false);
const threadForm = ref({ name: "", type: "direct", members: [] });
const showMessages = ref(false);
const activeThread = ref(null);
const threadMessages = ref([]);
const newMessage = ref("");

// Users for assignment
const userOptions = ref([]);
// Jobs + customers for task linking (Doug 2026-04-13: tasks can link to either, both, or neither)
const jobOptions = ref([]);
const customerOptions = ref([]);

const totalUnread = computed(() => threads.value.reduce((s, t) => s + (t.unread || 0), 0));

function shortDate(d) { if (!d) return ""; try { return new Date(d).toLocaleDateString("en-US", { month: "short", day: "numeric" }); } catch { return d; } }

// Resolve UUIDs on task cards to human-readable labels using the already-
// loaded job/customer option lists. Returns empty string when the task has
// no link OR when the label hasn't loaded yet (fails silent so the card
// just hides that meta row).
function jobLabelFor(jobId) {
  if (!jobId) return "";
  const hit = jobOptions.value.find((o) => o.value === jobId);
  return hit ? hit.label : "";
}
function customerLabelFor(customerId) {
  if (!customerId) return "";
  const hit = customerOptions.value.find((o) => o.value === customerId);
  return hit ? hit.label : "";
}
function shortTime(d) { if (!d) return ""; try { return new Date(d).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }); } catch { return d; } }

async function loadTasks() {
  tasksLoading.value = true;
  try {
    const isCompleted = taskView.value === "completed";
    const view = isCompleted ? "all" : taskView.value;
    const bucket = isCompleted ? "completed" : "active";
    const data = await api.get(
      `/api/planner/tasks?view=${view}&bucket=${bucket}&sort=${taskSort.value}`,
    );
    tasks.value = (data.items || []).map((t) => ({ ...t, _done: t.status === "done" }));
  } catch { tasks.value = []; } finally { tasksLoading.value = false; }
}

async function loadPlans() {
  plansLoading.value = true;
  try { const data = await api.get("/api/planner/plans"); plans.value = data.items || []; }
  catch { plans.value = []; } finally { plansLoading.value = false; }
}

async function loadThreads() {
  threadsLoading.value = true;
  try { const data = await api.get("/api/planner/threads"); threads.value = data.items || []; }
  catch { threads.value = []; } finally { threadsLoading.value = false; }
}

async function loadUsers() {
  try {
    const data = await api.get("/api/users");
    const list = Array.isArray(data) ? data : data?.items || [];
    userOptions.value = list.map((u) => ({ label: u.username || u.email || u.id, value: u.id }));
  } catch { userOptions.value = []; }
}

async function loadJobsAndCustomers() {
  try {
    const data = await api.get("/api/jobs");
    const list = Array.isArray(data) ? data : data?.items || [];
    jobOptions.value = list.map((j) => ({
      label: j.title || j.id,
      value: j.id,
    }));
  } catch { jobOptions.value = []; }
  try {
    const data = await api.get("/api/customers");
    const list = Array.isArray(data) ? data : data?.items || [];
    customerOptions.value = list.map((c) => ({
      label: c.name || c.email || c.id,
      value: c.id,
    }));
  } catch { customerOptions.value = []; }
}

async function createTask() {
  taskSaving.value = true;
  try {
    const due = taskForm.value.due_date instanceof Date ? taskForm.value.due_date.toISOString().slice(0, 10) : taskForm.value.due_date;
    await api.post("/api/planner/tasks", { ...taskForm.value, due_date: due }, { successMessage: "Task created" });
    showTaskForm.value = false;
    taskForm.value = {
      title: "",
      description: "",
      priority: "low",
      due_date: null,
      assigned_to: null,
      job_id: null,
      customer_id: null,
    };
    await loadTasks();
  } finally { taskSaving.value = false; }
}

async function toggleTask(task) {
  const newStatus = task._done ? "done" : "todo";
  await api.patch(`/api/planner/tasks/${task.id}`, { status: newStatus });
  task.status = newStatus;
  // Done tasks leave the active buckets and enter the Completed tab;
  // refresh so the row leaves/enters the visible list immediately.
  await loadTasks();
}

function editTask(task) {
  // Clone so dialog edits don't mutate the list row until Save commits.
  // Description was the visible-data omission Doug flagged 2026-05-03 —
  // the card meta hides description by design (would crowd the list), so
  // the detail dialog is the only surface that shows it.
  selectedTask.value = {
    id: task.id,
    title: task.title || "",
    description: task.description || "",
    priority: task.priority || "low",
    status: task.status || "todo",
    due_date: task.due_date || null,
    assigned_to: task.assigned_to || null,
    job_id: task.job_id || null,
    customer_id: task.customer_id || null,
  };
  showTaskDetail.value = true;
}

async function saveTaskEdits() {
  if (!selectedTask.value) return;
  taskEditSaving.value = true;
  try {
    const t = selectedTask.value;
    const payload = {
      title: t.title,
      description: t.description,
      priority: t.priority,
      status: t.status,
      due_date: t.due_date,
      assigned_to: t.assigned_to,
      job_id: t.job_id,
      customer_id: t.customer_id,
    };
    await api.patch(`/api/planner/tasks/${t.id}`, payload, { successMessage: "Task updated" });
    showTaskDetail.value = false;
    selectedTask.value = null;
    await loadTasks();
  } finally {
    taskEditSaving.value = false;
  }
}

async function createPlan() {
  planSaving.value = true;
  try {
    await api.post("/api/planner/plans", planForm.value, { successMessage: "Plan created" });
    showPlanForm.value = false;
    planForm.value = { title: "", description: "", is_template: false };
    await loadPlans();
  } finally { planSaving.value = false; }
}

async function createThread() {
  try {
    await api.post("/api/planner/threads", threadForm.value, { successMessage: "Conversation started" });
    showThreadForm.value = false;
    threadForm.value = { name: "", type: "direct", members: [] };
    await loadThreads();
  } catch { /* handled */ }
}

async function openThread(thread) {
  activeThread.value = thread;
  showMessages.value = true;
  try {
    const data = await api.get(`/api/planner/threads/${thread.id}/messages`);
    threadMessages.value = data.items || [];
    thread.unread = 0;
  } catch { threadMessages.value = []; }
}

async function sendMessage() {
  if (!newMessage.value.trim() || !activeThread.value) return;
  try {
    await api.post(`/api/planner/threads/${activeThread.value.id}/messages`, { body: newMessage.value });
    newMessage.value = "";
    const data = await api.get(`/api/planner/threads/${activeThread.value.id}/messages`);
    threadMessages.value = data.items || [];
  } catch { /* handled */ }
}

watch([taskView, taskSort], loadTasks);
watch(activeTab, (tab) => {
  if (tab === "plans" && !plans.value.length) loadPlans();
  if (tab === "messages" && !threads.value.length) loadThreads();
});

onMounted(() => { loadTasks(); loadUsers(); loadJobsAndCustomers(); });
</script>

<style scoped>
.page-title { margin: 0; }
.tab-content { margin-top: 1rem; }
.loading { display: flex; justify-content: center; padding: 2rem; }
.empty { text-align: center; padding: 2rem; color: var(--p-text-muted-color); }
.w-full { width: 100%; }
.flex-1 { flex: 1; }
.form-stack { display: flex; flex-direction: column; gap: 0.75rem; }
.form-row { display: flex; gap: 0.75rem; align-items: center; }

/* Tasks */
.task-filters { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; flex-wrap: wrap; }
.sort-select { min-width: 140px; }
.task-list { display: flex; flex-direction: column; gap: 0.4rem; }
.task-card { display: flex; align-items: flex-start; gap: 0.75rem; padding: 0.6rem 0.75rem; background: var(--p-content-hover-background); border: 1px solid var(--p-content-border-color); border-radius: 8px; cursor: pointer; }
.task-card:hover { border-color: var(--p-primary-color); }
.task-card.urgent { border-left: 3px solid #ef4444; }
.task-card.high { border-left: 3px solid #f59e0b; }
.task-body { flex: 1; }
.task-title { font-weight: 600; }
.task-title.done { text-decoration: line-through; opacity: 0.5; }
.task-meta { display: flex; gap: 0.5rem; align-items: center; margin-top: 0.25rem; flex-wrap: wrap; }
.meta-item { font-size: 0.78rem; color: var(--p-text-muted-color); display: flex; align-items: center; gap: 0.2rem; }

/* Plans */
.plan-list { display: flex; flex-direction: column; gap: 0.5rem; }
.plan-card { padding: 0.75rem; background: var(--p-content-hover-background); border: 1px solid var(--p-content-border-color); border-radius: 8px; cursor: pointer; }
.plan-card:hover { border-color: var(--p-primary-color); }
.plan-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.plan-progress { display: flex; align-items: center; gap: 0.5rem; }
.progress-bar { flex: 1; height: 6px; background: var(--p-content-border-color); border-radius: 3px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--p-primary-color); border-radius: 3px; transition: width 0.3s; }
.progress-text { font-size: 0.8rem; color: var(--p-text-muted-color); font-weight: 600; }

/* Threads */
.thread-list { display: flex; flex-direction: column; gap: 0.4rem; }
.thread-card { display: flex; align-items: center; gap: 0.75rem; padding: 0.6rem 0.75rem; background: var(--p-content-hover-background); border: 1px solid var(--p-content-border-color); border-radius: 8px; cursor: pointer; }
.thread-card:hover { border-color: var(--p-primary-color); }
.thread-name { font-weight: 600; display: flex; align-items: center; gap: 0.4rem; }
.thread-preview { flex: 1; font-size: 0.82rem; color: var(--p-text-muted-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Messages */
.messages-body { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.5rem; padding: 0.5rem; min-height: 300px; }
.msg { max-width: 80%; padding: 0.5rem 0.75rem; border-radius: 12px; background: var(--p-content-hover-background); border: 1px solid var(--p-content-border-color); }
.msg.mine { align-self: flex-end; background: var(--p-primary-color); color: white; border-color: transparent; }
.msg-sender { font-size: 0.7rem; font-weight: 700; opacity: 0.7; }
.msg-text { font-size: 0.9rem; }
.msg-time { font-size: 0.65rem; opacity: 0.5; text-align: right; }
.msg-input-row { display: flex; gap: 0.5rem; width: 100%; }
</style>
