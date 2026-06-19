<template>
    <section class="duplicates-view view-card">
      <div class="header-row">
        <div>
          <h2>Customer Duplicates</h2>
          <p class="subtitle">
            Groups of customers sharing the same name. Pick one to keep —
            every job, invoice, and reference on the others will move to the
            keeper. The merged-away records are soft-deleted (reversible).
          </p>
        </div>
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          severity="secondary"
          @click="loadGroups"
          :disabled="isLoading"
          data-testid="refresh-duplicates-btn"
        />
      </div>

      <div v-if="isLoading" class="spinner-wrap" data-testid="duplicates-loading">
        <ProgressSpinner />
      </div>

      <div v-else-if="!groups.length" class="empty-message" data-testid="duplicates-empty">
        No duplicate customers found.
      </div>

      <div v-else class="groups-list" data-testid="duplicates-groups">
        <Card v-for="group in groups" :key="group.normalized_name" class="group-card">
          <template #title>
            <span class="group-title">{{ group.members[0].name }}</span>
            <Tag :value="`${group.count} records`" severity="warn" class="ml-2" />
          </template>
          <template #content>
            <DataTable
      responsiveLayout="scroll" :value="group.members" stripedRows class="member-table">
              <Column header="Keep" style="width: 70px">
                <template #body="{ data }">
                  <RadioButton
                    v-model="selections[group.normalized_name].keep"
                    :value="data.id"
                    :name="`keep-${group.normalized_name}`"
                    :data-testid="`keep-${data.id}`"
                  />
                </template>
              </Column>
              <Column header="Merge" style="width: 80px">
                <template #body="{ data }">
                  <Checkbox
                    v-if="selections[group.normalized_name].keep !== data.id"
                    v-model="selections[group.normalized_name].merge"
                    :value="data.id"
                    :data-testid="`merge-${data.id}`"
                  />
                </template>
              </Column>
              <Column header="Name">
                <template #body="{ data }">
                  <router-link :to="`/customers/${data.id}`" class="name-link" target="_blank">
                    {{ data.name }}
                  </router-link>
                </template>
              </Column>
              <Column field="phone" header="Phone">
                <template #body="{ data }">
                  <span v-if="data.phone">{{ data.phone }}</span>
                  <span v-else class="text-muted">—</span>
                </template>
              </Column>
              <Column field="email" header="Email">
                <template #body="{ data }">
                  <span v-if="data.email">{{ data.email }}</span>
                  <span v-else class="text-muted">—</span>
                </template>
              </Column>
              <Column field="job_count" header="Jobs" style="width: 70px" />
              <Column field="invoice_count" header="Invoices" style="width: 90px" />
              <Column header="QB" style="width: 50px">
                <template #body="{ data }">
                  <i v-if="data.has_qb_link" class="pi pi-check qb-yes" title="Linked to QuickBooks" />
                  <span v-else class="text-muted">—</span>
                </template>
              </Column>
              <Column field="created_at" header="Created" style="width: 130px">
                <template #body="{ data }">
                  <span class="text-muted">{{ formatDate(data.created_at) }}</span>
                </template>
              </Column>
            </DataTable>

            <div class="group-actions">
              <Button
                label="Merge selected into keeper"
                icon="pi pi-compress"
                severity="danger"
                :disabled="!canMerge(group) || merging === group.normalized_name"
                :loading="merging === group.normalized_name"
                @click="confirmMerge(group)"
                :data-testid="`merge-btn-${group.normalized_name}`"
              />
              <span v-if="selections[group.normalized_name].keep" class="hint">
                Keep: <strong>{{ keeperName(group) }}</strong>
                · Merging:
                <strong>{{ selections[group.normalized_name].merge.length }}</strong>
              </span>
              <span v-else class="hint text-muted">Pick one to keep, then check the ones to merge.</span>
            </div>
          </template>
        </Card>
      </div>

      <Dialog
        v-model:visible="showConfirm"
        header="Confirm merge"
        :modal="true"
        :style="{ width: '480px' }"
      >
        <p>
          Move all jobs, invoices, and references from
          <strong>{{ pendingMerge?.merge_count }}</strong> record(s) onto
          <strong>{{ pendingMerge?.keep_name }}</strong>?
        </p>
        <p class="text-muted small">
          The merged-away customers will be soft-deleted (deleted_at set).
          Reversible via audit log — no data is lost.
        </p>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showConfirm = false" />
          <Button label="Merge" severity="danger" @click="doMerge" />
        </template>
      </Dialog>

      <Toast />
    </section>
</template>

<script setup>
import { onMounted, ref, reactive } from "vue";
import { useToast } from "primevue/usetoast";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Card from "primevue/card";
import Checkbox from "primevue/checkbox";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import ProgressSpinner from "primevue/progressspinner";
import RadioButton from "primevue/radiobutton";
import Tag from "primevue/tag";
import Toast from "primevue/toast";

const api = useApiWithToast();
const toast = useToast();

const isLoading = ref(false);
const groups = ref([]);
const selections = reactive({});
const merging = ref(null);
const showConfirm = ref(false);
const pendingMerge = ref(null);

async function loadGroups() {
  isLoading.value = true;
  try {
    const data = await api.get("/api/customers/duplicates");
    groups.value = data?.groups || [];
    for (const g of groups.value) {
      if (!selections[g.normalized_name]) {
        selections[g.normalized_name] = { keep: null, merge: [] };
      }
    }
  } finally {
    isLoading.value = false;
  }
}

function canMerge(group) {
  const sel = selections[group.normalized_name];
  return !!sel.keep && sel.merge.length > 0;
}

function keeperName(group) {
  const sel = selections[group.normalized_name];
  const k = group.members.find((m) => m.id === sel.keep);
  return k ? k.name : "";
}

function confirmMerge(group) {
  const sel = selections[group.normalized_name];
  pendingMerge.value = {
    group_key: group.normalized_name,
    keep_id: sel.keep,
    merge_ids: [...sel.merge],
    keep_name: keeperName(group),
    merge_count: sel.merge.length,
  };
  showConfirm.value = true;
}

async function doMerge() {
  if (!pendingMerge.value) return;
  const { group_key, keep_id, merge_ids } = pendingMerge.value;
  merging.value = group_key;
  showConfirm.value = false;
  try {
    const result = await api.post("/api/customers/merge", { keep_id, merge_ids });
    toast.add({
      severity: "success",
      summary: "Merged",
      detail: `${result.merged_count} record(s) merged. Rows updated: ${Object.keys(
        result.rows_updated || {}
      ).length} tables.`,
      life: 4000,
    });
    await loadGroups();
  } finally {
    merging.value = null;
    pendingMerge.value = null;
  }
}

function formatDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString();
  } catch {
    return iso;
  }
}

onMounted(loadGroups);
</script>

<style scoped>
.duplicates-view {
  padding: 1.5rem;
}
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 1.5rem;
  gap: 1rem;
}
.header-row h2 {
  margin: 0 0 0.25rem;
}
.subtitle {
  margin: 0;
  color: var(--p-text-muted-color);
  max-width: 60ch;
}
.groups-list {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.group-card {
  border: 1px solid var(--surface-border);
}
.group-title {
  font-size: 1.1rem;
  font-weight: 600;
}
.ml-2 {
  margin-left: 0.5rem;
}
.member-table {
  margin: 0.5rem 0 1rem;
}
.group-actions {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding-top: 0.5rem;
}
.hint {
  font-size: 0.9rem;
}
.text-muted {
  color: var(--p-text-muted-color);
}
.qb-yes {
  color: var(--p-green-500);
}
.name-link {
  color: var(--p-primary-color);
  text-decoration: none;
}
.name-link:hover {
  text-decoration: underline;
}
.empty-message {
  padding: 2rem;
  text-align: center;
  color: var(--p-text-muted-color);
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem;
}
.small {
  font-size: 0.85rem;
}
</style>
