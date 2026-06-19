<template>
  <div class="bug-report-widget">
    <Button rounded severity="secondary" class="bug-btn"
      v-tooltip.left="'Report a Bug'" @click="showDialog = true" data-testid="bug-report-btn">
      <span style="font-size:1.4rem">🐛</span>
    </Button>

    <Dialog v-model:visible="showDialog" header="Report a Bug" :style="{ width: '460px' }" modal>
      <div class="flex flex-column gap-3 mt-2">
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Subject *</label>
          <InputText v-model="form.subject" placeholder="Brief summary of the issue" data-testid="bug-subject" />
        </div>
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Priority</label>
          <Select v-model="form.priority" :options="priorities" data-testid="bug-priority" />
        </div>
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Description *</label>
          <Textarea v-model="form.description" rows="4" placeholder="Steps to reproduce the issue..."
            data-testid="bug-description" />
        </div>
        <div class="meta-info">
          <small><strong>Page:</strong> {{ form.page_url }}</small><br>
          <small><strong>Browser:</strong> {{ shortBrowser }}</small>
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="showDialog = false" />
        <Button label="Submit Report" icon="pi pi-send" :loading="submitting"
          :disabled="!form.subject || !form.description" @click="submit" data-testid="bug-submit" />
      </template>
    </Dialog>
  </div>
</template>

<script setup>
import { ref, computed, watch } from "vue";
import { useToast } from "primevue/usetoast";
import { useApi } from "../composables/useApi";
import Button from "primevue/button";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import Select from "primevue/select";

const toast = useToast();
const api = useApi();
const showDialog = ref(false);
const submitting = ref(false);
const priorities = ["low", "medium", "high", "critical"];

const form = ref({
  subject: "",
  description: "",
  priority: "medium",
  page_url: "",
  browser_info: "",
});

const shortBrowser = computed(() => {
  const ua = form.value.browser_info;
  if (ua.includes("Chrome")) return "Chrome";
  if (ua.includes("Firefox")) return "Firefox";
  if (ua.includes("Safari")) return "Safari";
  return ua.slice(0, 50);
});

watch(showDialog, (open) => {
  if (open) {
    form.value.page_url = window.location.href;
    form.value.browser_info = navigator.userAgent;
  }
});

async function submit() {
  submitting.value = true;
  try {
    // Tenant-plane record (legacy /api/feedback/bug-report flow).
    await api.post("/api/feedback/bug-report", {
      subject: form.value.subject,
      description: form.value.description,
      priority: form.value.priority,
      page_url: form.value.page_url,
      browser_info: form.value.browser_info,
    });
    // Control-plane mirror (cc2-s49a) — surfaces the report in the
    // apartment-manager cockpit's support queue. Best-effort: if the
    // CP write fails, the tenant-plane record still landed.
    const ccPriority = form.value.priority === "critical" ? "urgent" : form.value.priority;
    const browserSummary = shortBrowser.value;
    const annotated = `${form.value.description}\n\n---\nPage: ${form.value.page_url}\nBrowser: ${browserSummary}`;
    api.post("/api/support/bug", {
      subject: form.value.subject,
      body: annotated,
      priority: ccPriority,
    }).catch(() => { /* CP write failed — tenant record is the source of truth */ });
    toast.add({ severity: "success", summary: "Bug Reported", detail: "Thank you! We'll look into it.", life: 4000 });
    showDialog.value = false;
    form.value.subject = "";
    form.value.description = "";
    form.value.priority = "medium";
  } catch {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to submit bug report", life: 4000 });
  } finally {
    submitting.value = false;
  }
}
</script>

<style>
.bug-report-widget {
  position: fixed !important;
  /* Sit above the mobile bottom nav (var --bottom-nav-height ≈ 64px),
     plus a comfortable gap. Desktop has no bottom nav so the same offset
     just feels right above the screen edge. */
  bottom: calc(var(--bottom-nav-height, 0px) + 1rem);
  right: 1rem;
  z-index: 9999;
}
@media (min-width: 768px) {
  .bug-report-widget {
    bottom: 1.5rem;
    right: 1.5rem;
  }
}
.bug-report-widget .p-button.p-button-rounded {
  width: 3.5rem !important;
  height: 3.5rem !important;
  opacity: 0.85;
  transition: all 0.2s;
  background: #ef4444 !important;
  border: 2px solid #fca5a5 !important;
  box-shadow: 0 4px 12px rgba(239, 68, 68, 0.4);
}
.bug-report-widget .p-button.p-button-rounded:hover {
  opacity: 1;
  background: #dc2626 !important;
  transform: scale(1.1);
  box-shadow: 0 6px 16px rgba(239, 68, 68, 0.6);
}
.bug-report-widget .p-button.p-button-rounded .p-button-icon {
  font-size: 1.2rem;
}
.meta-info { padding: 0.5rem; background: var(--surface-ground); border-radius: 6px; opacity: 0.7; }
</style>
