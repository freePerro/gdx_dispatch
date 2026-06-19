<script setup>
import { ref, computed } from 'vue'
import { useApi } from '../composables/useApi'

defineProps({
  showHeading: { type: Boolean, default: true },
})

const api = useApi()

const RED_TOOLS = ['invoices.void']

const question = ref('')
const lastQuestion = ref('')
const loading = ref(false)
const error = ref(null)
const latestResponse = ref(null)
const toolsUsedExpanded = ref(false)
const redConfirmInput = ref('')
const history = ref([])

const isRedAction = computed(() => {
  const pending = latestResponse.value?.pending_action
  return pending && RED_TOOLS.includes(pending.tool)
})

const expectedConfirmationString = computed(() => {
  const pending = latestResponse.value?.pending_action
  if (!pending) return ''
  if (pending.diff?.invoice_number) return pending.diff.invoice_number
  if (pending.diff) {
    const stringFields = Object.entries(pending.diff).filter(([_, v]) => typeof v === 'string')
    if (stringFields.length > 0) return stringFields[0][1]
  }
  return `${pending.tool}:${JSON.stringify(pending.payload)}`
})

const addToHistory = (q, resp) => {
  if (resp.answer !== null && resp.answer !== undefined) {
    history.value.push({
      question: q,
      answer: resp.answer,
      tools_used: resp.tools_used || [],
      timestamp: new Date().toLocaleTimeString()
    })
    if (history.value.length > 50) history.value.shift()
  }
}

const handleSubmit = async () => {
  if (!question.value.trim()) return
  loading.value = true
  error.value = null
  latestResponse.value = null
  toolsUsedExpanded.value = false
  redConfirmInput.value = ''
  try {
    const priorHistory = history.value.slice(-10).map(h => ({
      question: h.question,
      answer: h.answer,
    }))
    const response = await api.post('/api/ai/ask', {
      question: question.value,
      history: priorHistory,
    })
    latestResponse.value = response
    lastQuestion.value = question.value
    addToHistory(question.value, response)
  } catch (err) {
    error.value = err.message || 'Request failed'
  } finally {
    loading.value = false
  }
}

const handleApply = async () => {
  if (!latestResponse.value?.pending_action) return
  loading.value = true
  error.value = null
  const { tool, payload, approval_token } = latestResponse.value.pending_action
  try {
    const priorHistory = history.value.slice(-10).map(h => ({
      question: h.question,
      answer: h.answer,
    }))
    const response = await api.post('/api/ai/ask', {
      question: lastQuestion.value,
      approval_ref: approval_token,
      tool, payload,
      history: priorHistory,
    })
    latestResponse.value = response
    toolsUsedExpanded.value = false
    redConfirmInput.value = ''
    addToHistory(lastQuestion.value, response)
  } catch (err) {
    error.value = err.message || 'Failed to apply action'
  } finally {
    loading.value = false
  }
}

const handleCancel = () => {
  if (latestResponse.value) {
    latestResponse.value = { ...latestResponse.value, pending_action: null }
  }
  redConfirmInput.value = ''
}

const isApplyEnabled = computed(() => {
  if (isRedAction.value) {
    return redConfirmInput.value.trim() === expectedConfirmationString.value.trim()
  }
  return true
})
</script>

<template>
  <div class="ai-assistant-container">
    <h1 v-if="showHeading">AI Assistant</h1>

    <div class="input-section">
      <textarea
        v-model="question"
        data-test="ai-question-input"
        placeholder="Ask a question..."
        rows="4"
      ></textarea>
      <button
        data-test="ai-submit"
        :disabled="!question.trim() || loading"
        @click="handleSubmit"
      >
        Submit
      </button>
    </div>

    <div v-if="loading" data-test="ai-loading" class="status-message">
      Thinking...
    </div>

    <div v-if="error" class="error-message">
      {{ error }}
    </div>

    <div v-if="latestResponse" class="response-section">
      <div v-if="latestResponse.disabled" class="warning-message">
        AI is disabled — please configure a key in Settings.
      </div>

      <div v-else-if="latestResponse.pending_action" class="pending-action-container">
        <div v-if="isRedAction" data-test="ai-red-modal" class="red-confirm-modal">
          <div class="modal-header">
            <strong>High-risk action: {{ latestResponse.pending_action.tool }}</strong>
          </div>
          <div class="modal-body">
            <p>Please type the following identifier to confirm:</p>
            <div class="confirmation-id">{{ expectedConfirmationString }}</div>
            <input
              v-model="redConfirmInput"
              data-test="ai-red-confirm-input"
              type="text"
              placeholder="Type identifier here..."
              class="confirm-input"
            />
          </div>
          <div class="card-actions">
            <button data-test="ai-red-cancel" class="cancel-btn" @click="handleCancel">Cancel</button>
            <button
              data-test="ai-red-apply"
              class="apply-btn red-apply-btn"
              :disabled="!isApplyEnabled"
              @click="handleApply"
            >
              Apply
            </button>
          </div>
        </div>

        <div v-else data-test="ai-yellow-confirm" class="yellow-confirm-card">
          <div data-test="ai-yellow-tool" class="tool-name">
            {{ latestResponse.pending_action.tool }}
          </div>
          <pre data-test="ai-yellow-diff" class="diff-view">{{ JSON.stringify(latestResponse.pending_action.diff, null, 2) }}</pre>
          <div class="card-actions">
            <button data-test="ai-yellow-apply" class="apply-btn" @click="handleApply">Apply</button>
            <button data-test="ai-yellow-cancel" class="cancel-btn" @click="handleCancel">Cancel</button>
          </div>
        </div>
      </div>

      <div v-else-if="latestResponse.answer" data-test="ai-answer" class="answer-text">
        {{ latestResponse.answer }}
      </div>

      <div
        v-if="latestResponse.tools_used?.length > 0"
        data-test="ai-tools-used"
        class="tools-panel"
      >
        <button
          data-test="ai-tools-used-toggle"
          class="tools-toggle"
          @click="toolsUsedExpanded = !toolsUsedExpanded"
        >
          Tools used ({{ latestResponse.tools_used.length }})
          <span class="toggle-icon">{{ toolsUsedExpanded ? '▲' : '▼' }}</span>
        </button>
        <ul v-if="toolsUsedExpanded" data-test="ai-tools-used-list" class="tools-list">
          <li v-for="tool in latestResponse.tools_used" :key="tool">{{ tool }}</li>
        </ul>
      </div>
    </div>

    <div data-test="ai-history" class="history-section">
      <div
        v-for="(item, index) in [...history].reverse()"
        :key="index"
        data-test="ai-history-item"
        class="history-item"
      >
        <div class="history-question"><strong>Q:</strong> {{ item.question }}</div>
        <div class="history-answer"><strong>A:</strong> {{ item.answer }}</div>
        <div v-if="item.timestamp" class="history-timestamp">{{ item.timestamp }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ai-assistant-container { max-width: 800px; margin: 0 auto; padding: 20px; }
.input-section { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
textarea { width: 100%; padding: 12px; border: 1px solid #ccc; border-radius: 4px; font-family: inherit; }
button { align-self: flex-end; padding: 8px 16px; cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: 0.6; }
.status-message, .error-message, .warning-message, .answer-text { padding: 15px; border-radius: 4px; }
.status-message { color: #666; }
.error-message { background-color: #fee2e2; color: #991b1b; }
.warning-message { background-color: #fef3c7; color: #92400e; }
.answer-text { background-color: #f3f4f6; color: #1f2937; white-space: pre-wrap; }
.yellow-confirm-card { background-color: #fef3c7; border: 1px solid #fcd34d; border-radius: 4px; padding: 15px; margin-top: 15px; }
.tool-name { font-weight: bold; margin-bottom: 10px; color: #92400e; }
.diff-view { background-color: #fffbeb; padding: 10px; border-radius: 4px; font-size: 0.875rem; overflow-x: auto; margin-bottom: 15px; border: 1px solid #fde68a; }
.card-actions { display: flex; gap: 10px; justify-content: flex-end; }
.apply-btn { background-color: #92400e; color: white; border: none; border-radius: 4px; padding: 6px 12px; }
.cancel-btn { background-color: transparent; border: 1px solid #92400e; color: #92400e; border-radius: 4px; padding: 6px 12px; }
.red-confirm-modal { background-color: #fef2f2; border: 1px solid #fecaca; border-radius: 4px; padding: 20px; margin-top: 15px; }
.modal-header { margin-bottom: 15px; color: #991b1b; font-size: 1.1rem; }
.modal-body { margin-bottom: 20px; }
.confirmation-id { font-family: monospace; background: #fee2e2; padding: 8px; border-radius: 4px; margin: 10px 0; font-weight: bold; color: #b91c1c; text-align: center; }
.confirm-input { width: 100%; padding: 10px; border: 1px solid #fca5a5; border-radius: 4px; box-sizing: border-box; }
.red-apply-btn { background-color: #b91c1c; }
.tools-panel { margin-top: 15px; border: 1px solid #e5e7eb; border-radius: 4px; overflow: hidden; }
.tools-toggle { width: 100%; align-self: flex-start; background: none; border: none; padding: 10px 15px; text-align: left; font-size: 0.875rem; color: #4b5563; display: flex; justify-content: space-between; align-items: center; cursor: pointer; }
.tools-toggle:hover { background-color: #f9fafb; }
.toggle-icon { font-size: 0.75rem; }
.tools-list { margin: 0; padding: 10px 15px 15px 35px; list-style-type: disc; background-color: #f9fafb; font-size: 0.875rem; color: #374151; }
.history-section { margin-top: 40px; border-top: 1px solid #e5e7eb; padding-top: 20px; }
.history-item { background-color: #f9fafb; border: 1px solid #e5e7eb; border-radius: 4px; padding: 12px; margin-bottom: 12px; }
.history-question, .history-answer { margin-bottom: 4px; font-size: 0.9rem; }
.history-timestamp { font-size: 0.75rem; color: #6b7280; text-align: right; }
</style>
