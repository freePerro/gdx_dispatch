<template>
    <section class="duplicates-view view-card">
      <div class="header-row">
        <div>
          <h2>Customer Duplicates</h2>
          <p class="subtitle">
            Groups of customers that share a name, an email address, or a
            phone number. Pick one to keep — every job, invoice, and reference
            on the others moves to the keeper. The merged-away records are
            soft-deleted, but there is <strong>no undo button</strong>:
            reversing a merge means reading the audit trail by hand.
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
            <!-- What tied these together decides how to treat them: records
                 sharing a NAME are usually the same account entered twice,
                 while records sharing only an EMAIL are often one account's
                 separate jobs (QuickBooks sub-customers) and must NOT be
                 blindly merged. Saying which is the difference between a
                 reviewer who can act and one who is guessing. -->
            <Tag
              :value="matchLabel(group)"
              :severity="group.match_on === 'name' ? 'danger' : 'info'"
              class="ml-2"
              :data-testid="`match-on-${group.match_on}`"
            />
          </template>
          <template #content>
            <div
              v-if="group.match_on === 'email' || group.match_on === 'phone'"
              class="match-hint"
              :data-testid="`match-hint-${group.match_on}`"
            >
              <p>
                These share
                {{ group.match_on === 'email' ? 'an email address' : 'a phone number' }}
                but have <strong>different names</strong>. That is usually one
                contact reached for several separate accounts — a builder, a
                property manager — or one account's separate jobs. Merging them
                moves real invoices onto the wrong customer, and there is no undo.
              </p>
              <div class="confirm-row">
                <Checkbox
                  v-model="selections[group.normalized_name].confirmed"
                  :binary="true"
                  :inputId="`confirm-${group.normalized_name}`"
                  :data-testid="`confirm-same-customer-${group.match_on}`"
                />
                <label :for="`confirm-${group.normalized_name}`">
                  I checked these records and they are the same customer.
                </label>
              </div>
            </div>
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
                  <span v-if="data.phone">{{ formatPhone(data.phone) }}</span>
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
              <!-- The right answer for a QuickBooks sub-customer. A merge
                   throws the losing row's NAME away; these names ARE the job
                   ("Site A", "Volden Field Shop"), and losing them loses the
                   only record of which job the work belonged to. This keeps
                   each one as a saved site on the keeper instead. -->
              <Button
                v-if="group.match_on !== 'name'"
                label="Make these jobsites of the keeper"
                icon="pi pi-map-marker"
                severity="secondary"
                outlined
                :disabled="!canMerge(group) || merging === group.normalized_name"
                :loading="merging === group.normalized_name"
                @click="confirmAbsorb(group)"
                :data-testid="`absorb-btn-${group.match_on}`"
              />
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

      <Dialog
        v-model:visible="showAbsorbConfirm"
        header="Keep these as jobsites?"
        :style="{ width: '480px' }"
        modal
        data-testid="absorb-confirm-dialog"
      >
        <p>
          <strong>{{ pendingAbsorb?.customer_ids?.length }}</strong> record(s)
          become saved sites on <strong>{{ pendingAbsorb?.keeper }}</strong>.
          Their jobs, estimates and invoices move to that account, and each
          name is kept as the site label.
        </p>
        <p class="muted">
          The records themselves are soft-deleted. There is no undo button —
          reversing this means reading the audit trail by hand.
        </p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showAbsorbConfirm = false" />
          <Button
            label="Keep as jobsites"
            icon="pi pi-map-marker"
            data-testid="confirm-absorb-btn"
            @click="doAbsorb"
          />
        </div>
      </Dialog>

      <Toast />
    </section>
</template>

<script setup>
import { onMounted, ref, reactive } from "vue";
import { useToast } from "primevue/usetoast";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDate, formatPhone } from "../composables/useFormatters";
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
const pendingAbsorb = ref(null);
const showAbsorbConfirm = ref(false);
const showConfirm = ref(false);
const pendingMerge = ref(null);

async function loadGroups() {
  isLoading.value = true;
  try {
    const data = await api.get("/api/customers/duplicates");
    groups.value = data?.groups || [];
    // Rebuild selections from scratch. Keeping them across a reload left the
    // just-merged (now soft-deleted) ids sitting in `.merge` under a group key
    // that still exists — so the merge button re-enabled itself with no
    // checkbox visibly ticked, and the next click 404'd on rows that were
    // already gone. Rare with name-only groups; routine now that groups run
    // to seven members.
    for (const key of Object.keys(selections)) delete selections[key];
    for (const g of groups.value) {
      selections[g.normalized_name] = { keep: null, merge: [], confirmed: false };
    }
  } finally {
    isLoading.value = false;
  }
}

function canMerge(group) {
  const sel = selections[group.normalized_name];
  if (!sel || !sel.keep || sel.merge.length === 0) return false;
  // Records grouped only by a shared email or phone are NOT known to be the
  // same customer. On this tenant the largest such group is seven separate
  // accounts reached through one contact at a builder — merging any two of
  // them destroys real billing history, and there is no unmerge. So the
  // reviewer has to say out loud that they checked.
  if (group.match_on !== "name" && !sel.confirmed) return false;
  return true;
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
  } catch (e) {
    // try/finally with NO catch is why a merge that 500'd read as "nothing
    // happened" instead of "it failed and here's why". This endpoint refuses
    // deliberately now (a partial retirement, a missing record), so the reason
    // is the useful half.
    toast.add({
      severity: "error",
      summary: "Not merged",
      detail: e?.message || "Could not merge these records.",
      life: 8000,
    });
  } finally {
    merging.value = null;
    pendingMerge.value = null;
  }
}

function confirmAbsorb(group) {
  const sel = selections[group.normalized_name];
  pendingAbsorb.value = {
    group_key: group.normalized_name,
    parent_id: sel.keep,
    customer_ids: [...sel.merge],
    keeper: keeperName(group),
  };
  showAbsorbConfirm.value = true;
}

async function doAbsorb() {
  if (!pendingAbsorb.value) return;
  const { group_key, parent_id, customer_ids } = pendingAbsorb.value;
  merging.value = group_key;
  showAbsorbConfirm.value = false;
  try {
    const result = await api.post(`/api/customers/${parent_id}/absorb`, { customer_ids });
    toast.add({
      severity: "success",
      summary: "Kept as jobsites",
      detail: `${result.sites.length} record(s) are now saved sites: ` +
        result.sites.map((s) => s.label).join(", "),
      life: 6000,
    });
    await loadGroups();
  } catch (e) {
    // try/finally with no catch showed the operator nothing on a 500 — and
    // the server refuses this deliberately (a record with its own invoices is
    // an account, not a jobsite), so the reason is the useful part.
    toast.add({
      severity: "error",
      summary: "Not folded",
      detail: e?.message || "Could not keep these as jobsites.",
      life: 8000,
    });
  } finally {
    merging.value = null;
    pendingAbsorb.value = null;
  }
}

onMounted(loadGroups);

function matchLabel(group) {
  // Explicit checks, not "anything that isn't name": an older cached response
  // without match_on would otherwise be labelled a phone match.
  if (group.match_on === "email") return `same email: ${group.match_value}`;
  if (group.match_on === "phone") return "same phone number";
  return "same name";
}
</script>

<style scoped>
.match-hint {
  margin: 0 0 12px;
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--p-yellow-500, #d4a017);
  font-size: 0.9rem;
}

.match-hint p {
  margin: 0 0 8px;
}

.confirm-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

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
