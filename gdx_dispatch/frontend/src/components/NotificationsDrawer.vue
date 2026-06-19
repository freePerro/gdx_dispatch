<template>
  <Drawer v-model:visible="visible" position="right" header="Notifications" class="notifications-drawer">
    <div v-if="store.loading" class="state-wrap"><ProgressSpinner /></div>
    <div v-else-if="!store.items.length" class="state-wrap empty">
      <i class="pi pi-inbox" style="font-size:2.5rem; color:#94a3b8;"></i>
      <p>No notifications</p>
    </div>
    <ul v-else class="notif-list">
      <li
        v-for="n in store.items"
        :key="n.id"
        class="notif-item"
        :class="{ unread: !n.is_read }"
        @click="onClick(n)"
      >
        <div class="notif-row">
          <span class="notif-title">{{ n.title }}</span>
          <span class="notif-time">{{ formatTime(n.created_at) }}</span>
        </div>
        <div class="notif-message">{{ n.message }}</div>
        <span v-if="n.category" class="notif-cat">{{ n.category }}</span>
      </li>
    </ul>
  </Drawer>
</template>

<script setup>
import { ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import Drawer from 'primevue/drawer';
import ProgressSpinner from 'primevue/progressspinner';
import { useNotificationsStore } from '../stores/notifications';

const props = defineProps({
  modelValue: { type: Boolean, default: false },
});
const emit = defineEmits(['update:modelValue']);

const router = useRouter();
const store = useNotificationsStore();
const visible = ref(props.modelValue);

watch(() => props.modelValue, (v) => {
  visible.value = v;
  if (v) store.fetchList();
});
watch(visible, (v) => {
  if (v !== props.modelValue) emit('update:modelValue', v);
});

// Category → destination map. The Notification model doesn't carry an
// entity_id, so deep-links land on the list-level route for that
// surface and the user picks the row. Mobile users get the mobile
// route; desktop users get the desktop route. Anything not mapped
// just closes the drawer (no dead navigation).
const _isMobileRoute = () => typeof window !== 'undefined'
  && /^\/mobile(\/|$)/.test(window.location.pathname);

function _destinationFor(category) {
  if (!category) return null;
  const mobile = _isMobileRoute();
  switch (category) {
    case 'lead':
      return '/leads';
    case 'job':
    case 'job_assigned':
    case 'job_status':
      return mobile ? '/mobile/jobs' : '/jobs';
    case 'invoice':
    case 'invoice_paid':
    case 'invoice_overdue':
      return mobile ? '/mobile/billing' : '/invoices';
    case 'estimate':
    case 'estimate_signed':
      return mobile ? '/mobile/estimates' : '/estimates';
    case 'customer':
      return mobile ? '/mobile/customers' : '/customers';
    case 'part':
    case 'part_shipped':
    case 'parts_to_order':
      return mobile ? '/mobile/parts-to-order' : '/purchase-orders';
    case 'inbox':
    case 'message':
      return mobile ? '/mobile/inbox' : '/communications';
    default:
      return null;
  }
}

function onClick(n) {
  if (!n.is_read) store.markRead(n.id);
  const dest = _destinationFor(n.category);
  if (dest) {
    visible.value = false;
    router.push(dest);
  } else {
    // No mapped destination — close the drawer so the click isn't a
    // dead-end. Pre-fix the drawer stayed open and the user couldn't
    // tell if their tap registered.
    visible.value = false;
  }
}

function formatTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now - d;
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffH = Math.floor(diffMin / 60);
    if (diffH < 24) return `${diffH}h ago`;
    return d.toLocaleDateString();
  } catch {
    return '';
  }
}
</script>

<style scoped>
.state-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 3rem 1rem;
  gap: 0.5rem;
  color: #64748b;
}
.notif-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.notif-item {
  padding: 0.85rem 1rem;
  border-bottom: 1px solid #e2e8f0;
  cursor: pointer;
  transition: background 0.15s ease;
}
.notif-item:hover {
  background: #f8fafc;
}
.notif-item.unread {
  background: #eff6ff;
  border-left: 3px solid var(--interactive-primary, #2563eb);
}
.notif-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
}
.notif-title {
  font-weight: 600;
  color: #0f172a;
}
.notif-time {
  font-size: 0.75rem;
  color: #64748b;
  white-space: nowrap;
}
.notif-message {
  margin-top: 0.25rem;
  color: #334155;
  font-size: 0.9rem;
}
.notif-cat {
  display: inline-block;
  margin-top: 0.4rem;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #64748b;
}
</style>
