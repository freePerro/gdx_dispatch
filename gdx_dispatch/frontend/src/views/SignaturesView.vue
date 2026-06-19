<template>
    <section class="signatures-view view-card" data-testid="signatures-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Signatures</h2>
        </template>
      </Toolbar>

      <Tabs value="all" class="tabview" data-testid="signatures-tabs">
        <TabList>
          <Tab v-for="tab in signatureTabs" :key="tab.key" :value="tab.key">
            {{ tab.label }}{{ tab.count ? ` (${tab.count})` : '' }}
          </Tab>
        </TabList>
        <TabPanels>
        <TabPanel
          v-for="tab in signatureTabs"
          :key="tab.key"
          :value="tab.key"
        >
          <div v-if="loading" class="spinner-wrap">
            <ProgressSpinner />
          </div>
          <DataTable
      responsiveLayout="scroll"
            v-else
            :value="filteredSignatures(tab.key)"
            paginator
            :rows="12"
            striped-rows
            class="clickable-row"
            data-testid="signatures-table"
          >
            <Column header="Preview" style="width:100px">
              <template #body="{ data }">
                <img
                  v-if="data.thumbnail_url"
                  :src="data.thumbnail_url"
                  alt="Signature thumbnail"
                  class="signature-thumb"
                />
                <span v-else class="muted">No preview</span>
              </template>
            </Column>
            <Column field="job_id" header="Job" style="width:120px" />
            <Column field="customer" header="Customer" />
            <Column field="signature_type" header="Type" style="width:140px">
              <template #body="{ data }">
                <Tag :value="data.signature_type?.toUpperCase()" :severity="typeSeverity(data.signature_type)" />
              </template>
            </Column>
            <Column field="signed_by" header="Signer" style="width:140px" />
            <Column field="signed_at" header="Signed" style="width:160px">
              <template #body="{ data }">{{ formatDate(data.signed_at) }}</template>
            </Column>
            <Column header="Actions" style="width:120px">
              <template #body="{ data }">
                <Button
                  icon="pi pi-eye"
                  text
                  size="small"
                  data-testid="signature-view-btn"
                  @click.stop="openSignature(data)"
                />
              </template>
            </Column>
          </DataTable>
        </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog v-model:visible="showDialog" header="Signature Details" modal :style="{ width: '560px' }" data-testid="signature-dialog">
        <div class="signature-dialog">
          <img
            v-if="selectedSignature?.signature_url"
            :src="selectedSignature.signature_url"
            alt="Full signature"
            class="signature-full"
          />
          <div v-else class="muted">No preview available</div>
          <div class="form-grid" style="margin-top:1rem;">
            <div class="form-field">
              <label>Job</label>
              <InputText :value="selectedSignature?.job_id" readonly />
            </div>
            <div class="form-field">
              <label>Customer</label>
              <InputText :value="selectedSignature?.customer" readonly />
            </div>
            <div class="form-field">
              <label>Signed At</label>
              <InputText :value="formatDate(selectedSignature?.signed_at)" readonly />
            </div>
            <div class="form-field">
              <label>Signer</label>
              <InputText :value="selectedSignature?.signed_by" readonly />
            </div>
            <div class="form-field full-width">
              <label>Notes</label>
              <Textarea :value="selectedSignature?.notes" rows="3" readonly />
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" @click="showDialog = false" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const signatures = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const selectedSignature = ref(null);

const signatureTabs = computed(() => {
  const tally = { all: signatures.value.length };
  signatures.value.forEach((sig) => {
    const key = sig.signature_type || "other";
    tally[key] = (tally[key] || 0) + 1;
  });
  return [
    { key: "all", label: "All", count: tally.all },
    { key: "proposal", label: "Proposal", count: tally.proposal || 0 },
    { key: "completion", label: "Completion", count: tally.completion || 0 },
    { key: "consent", label: "Consent", count: tally.consent || 0 },
  ];
});

const filteredSignatures = (type) => {
  if (type === "all") return signatures.value;
  return signatures.value.filter((sig) => sig.signature_type === type);
};

function formatDate(value) {
  return value ? value.split("T")[0] : "—";
}

function typeSeverity(type) {
  return {
    proposal: "info",
    completion: "success",
    consent: "warning",
  }[type] || "secondary";
}

async function loadSignatures() {
  loading.value = true;
  try {
    const data = await api.get("/api/signatures/pending");
    signatures.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function openSignature(signature) {
  selectedSignature.value = signature;
  showDialog.value = true;
}

onMounted(loadSignatures);
</script>
