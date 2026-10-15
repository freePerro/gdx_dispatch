<template>
  <span v-if="tags && tags.length" class="customer-tag-chips">
    <span
      v-for="tag in tags"
      :key="tag.id"
      class="customer-tag-chip"
      :style="{ backgroundColor: tag.color || DEFAULT_COLOR, color: readableText(tag.color || DEFAULT_COLOR) }"
      data-testid="customer-tag-chip"
    >{{ tag.name }}</span>
  </span>
  <span v-else class="text-muted">—</span>
</template>

<script setup>
// A customer's tags as coloured chips, for the Customers list and the
// Segments customer table (GDXA-1046). `tags` is the [{id, name, color}] both
// GET /api/customers and GET /api/segments/{id}/customers return.
defineProps({
  tags: { type: Array, default: () => [] },
});

// The Tags page's default colour (TagsView emptyForm).
const DEFAULT_COLOR = "#6366f1";

// Same luminance cut as DispatchView's area chips: dark text on light tags.
function readableText(hex) {
  const c = String(hex || "").replace("#", "");
  if (c.length !== 6) return "#ffffff";
  const r = parseInt(c.slice(0, 2), 16);
  const g = parseInt(c.slice(2, 4), 16);
  const b = parseInt(c.slice(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.6 ? "#1f2937" : "#ffffff";
}
</script>

<style scoped>
.customer-tag-chips {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.customer-tag-chip {
  display: inline-block;
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  line-height: 1.4;
  white-space: nowrap;
}

.text-muted {
  color: var(--text-muted, #94a3b8);
}
</style>
