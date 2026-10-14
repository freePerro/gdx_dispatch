<script setup>
import {
  acknowledgeLocationDisclosure,
} from '../lib/nativeGps';

const props = defineProps({
  visible: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(['update:visible', 'accepted', 'declined']);

function handleAccept() {
  acknowledgeLocationDisclosure();
  emit('accepted');
  emit('update:visible', false);
}

function handleDecline() {
  emit('declined');
  emit('update:visible', false);
}
</script>

<template>
  <div
    v-if="visible"
    class="disclosure-overlay"
    role="dialog"
    aria-modal="true"
    aria-labelledby="location-disclosure-title"
    data-test="mobile-location-disclosure-dialog"
  >
    <div class="disclosure-modal">
      <div class="disclosure-header">
        <div class="icon-bubble">
          <i class="pi pi-map-marker" aria-hidden="true" />
        </div>
        <h2 id="location-disclosure-title">Location Tracking & Privacy</h2>
      </div>

      <div class="disclosure-body">
        <p class="lead-text">
          <strong>GDX Dispatch collects location data</strong> to enable real-time dispatch routing, job arrival time estimation, and customer updates <strong>ONLY while you are clocked in on your active shift</strong>, even when the app is in the background or the screen is locked.
        </p>

        <div class="disclosure-bullets">
          <div class="bullet-row">
            <span class="bullet-icon">⏱️</span>
            <div>
              <strong>Shift-Bound Only:</strong> Tracking starts when you clock in and automatically terminates when you clock out or take a break.
            </div>
          </div>

          <div class="bullet-row">
            <span class="bullet-icon">🔒</span>
            <div>
              <strong>Internal & Private:</strong> Location data is used strictly by your company's dispatch board. It is never sold or shared with third parties.
            </div>
          </div>

          <div class="bullet-row">
            <span class="bullet-icon">🔔</span>
            <div>
              <strong>Ongoing Notice:</strong> A persistent Android notification will always be visible whenever location tracking is active.
            </div>
          </div>
        </div>
      </div>

      <div class="disclosure-actions">
        <button
          type="button"
          class="btn-accept"
          data-test="btn-accept-location"
          @click="handleAccept"
        >
          Accept & Enable Location
        </button>
        <button
          type="button"
          class="btn-decline"
          data-test="btn-decline-location"
          @click="handleDecline"
        >
          Not Now
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.disclosure-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  background-color: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.25rem;
}

.disclosure-modal {
  background-color: #111827;
  color: #f9fafb;
  border: 1px solid #374151;
  border-radius: 1rem;
  max-width: 28rem;
  width: 100%;
  box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
  overflow: hidden;
}

.disclosure-header {
  padding: 1.5rem 1.5rem 0.75rem;
  display: flex;
  align-items: center;
  gap: 0.875rem;
}

.icon-bubble {
  width: 2.75rem;
  height: 2.75rem;
  border-radius: 50%;
  background-color: rgba(37, 99, 235, 0.2);
  color: #60a5fa;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.25rem;
  flex-shrink: 0;
}

.disclosure-header h2 {
  font-size: 1.125rem;
  font-weight: 700;
  margin: 0;
  color: #f3f4f6;
}

.disclosure-body {
  padding: 0.5rem 1.5rem 1.25rem;
  font-size: 0.875rem;
  line-height: 1.45;
  color: #d1d5db;
}

.lead-text {
  margin-bottom: 1rem;
}

.lead-text strong {
  color: #f9fafb;
}

.disclosure-bullets {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  background-color: #1f2937;
  padding: 0.875rem;
  border-radius: 0.5rem;
}

.bullet-row {
  display: flex;
  align-items: flex-start;
  gap: 0.625rem;
  font-size: 0.8125rem;
}

.bullet-icon {
  font-size: 1rem;
  flex-shrink: 0;
}

.bullet-row strong {
  color: #e5e7eb;
}

.disclosure-actions {
  padding: 1rem 1.5rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.625rem;
  border-top: 1px solid #1f2937;
}

.btn-accept {
  width: 100%;
  padding: 0.75rem 1rem;
  background-color: #2563eb;
  color: #ffffff;
  border: none;
  border-radius: 0.5rem;
  font-size: 0.9375rem;
  font-weight: 600;
  cursor: pointer;
  min-height: 44px;
}

.btn-accept:active {
  background-color: #1d4ed8;
}

.btn-decline {
  width: 100%;
  padding: 0.625rem 1rem;
  background: transparent;
  color: #9ca3af;
  border: 1px solid #374151;
  border-radius: 0.5rem;
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  min-height: 44px;
}

.btn-decline:active {
  background-color: #1f2937;
  color: #d1d5db;
}
</style>
