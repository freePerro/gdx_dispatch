<template>
  <div class="folder-tree-node">
    <div
      class="folder-item"
      :class="{ active: selectedId === node.id, 'drop-target': isDropTarget }"
      :style="{ paddingLeft: `${0.4 + depth * 0.9}rem` }"
      :title="node.name"
      role="treeitem"
      tabindex="0"
      :aria-expanded="hasChildren ? expanded : undefined"
      :aria-selected="selectedId === node.id"
      :draggable="canMove"
      @click="$emit('select', node)"
      @keydown="onRowKeydown"
      @dragstart.stop="onDragStart"
      @dragover.prevent="onDragOver"
      @dragenter.prevent="onDragEnter"
      @dragleave="onDragLeave"
      @drop.stop.prevent="onDrop"
    >
      <button
        v-if="hasChildren"
        class="folder-toggle"
        :title="expanded ? 'Collapse' : 'Expand'"
        @click.stop="expanded = !expanded"
      >
        <i :class="expanded ? 'pi pi-chevron-down' : 'pi pi-chevron-right'"></i>
      </button>
      <span v-else class="folder-toggle-spacer"></span>
      <i class="pi pi-folder folder-icon"></i>
      <span class="folder-name">{{ node.name }}</span>
      <span v-if="node.doc_count > 0" class="folder-count">{{ node.doc_count }}</span>
      <button
        v-if="canDelete"
        class="folder-delete-btn"
        :title="`Delete folder &quot;${node.name}&quot;`"
        :data-testid="`folder-delete-${node.id}`"
        @click.stop="$emit('delete', node)"
      >
        <i class="pi pi-trash"></i>
      </button>
    </div>
    <div v-if="expanded && hasChildren" class="folder-children" role="group">
      <FolderTreeNode
        v-for="child in node.children"
        :key="child.id"
        :node="child"
        :selected-id="selectedId"
        :can-delete="canDelete"
        :can-move="canMove"
        :depth="depth + 1"
        @select="(n) => $emit('select', n)"
        @delete="(n) => $emit('delete', n)"
        @move="(payload) => $emit('move', payload)"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue';

const props = defineProps({
  node: { type: Object, required: true },
  selectedId: { type: String, default: null },
  canDelete: { type: Boolean, default: false },
  canMove: { type: Boolean, default: false },
  depth: { type: Number, default: 0 },
});

const emit = defineEmits(['select', 'delete', 'move']);

const hasChildren = computed(() => Boolean(props.node.children && props.node.children.length));
const expanded = ref(props.depth < 1);
const isDropTarget = ref(false);

const DRAG_MIME = 'application/x-gdx-folder-id';

// Keyboard path for the row: Enter/Space select (same as click), ArrowRight
// expands and ArrowLeft collapses via the same local `expanded` ref the
// chevron button toggles. Drag-to-move stays mouse-only.
function onRowKeydown(e) {
  if (e.key === 'Enter') {
    emit('select', props.node);
  } else if (e.key === ' ') {
    e.preventDefault();
    emit('select', props.node);
  } else if (e.key === 'ArrowRight') {
    if (hasChildren.value && !expanded.value) expanded.value = true;
  } else if (e.key === 'ArrowLeft') {
    if (expanded.value) expanded.value = false;
  }
}

function onDragStart(e) {
  if (!props.canMove) return;
  e.dataTransfer.setData(DRAG_MIME, props.node.id);
  e.dataTransfer.effectAllowed = 'move';
}

function onDragOver(e) {
  if (e.dataTransfer.types.includes(DRAG_MIME)) {
    e.dataTransfer.dropEffect = 'move';
  }
}

function onDragEnter(e) {
  if (e.dataTransfer.types.includes(DRAG_MIME)) {
    isDropTarget.value = true;
  }
}

function onDragLeave() {
  isDropTarget.value = false;
}

function onDrop(e) {
  isDropTarget.value = false;
  const draggedId = e.dataTransfer.getData(DRAG_MIME);
  if (!draggedId || draggedId === props.node.id) return;
  emit('move', { folderId: draggedId, newParentId: props.node.id });
}
</script>

<style scoped>
.folder-tree-node {
  display: flex;
  flex-direction: column;
}

.folder-item {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.5rem;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.875rem;
  line-height: 1.25;
  user-select: none;
  position: relative;
  border-left: 3px solid transparent;
}

.folder-item:hover {
  background: var(--p-content-hover-background, rgba(255, 255, 255, 0.04));
}

.folder-item:focus-visible {
  outline: 2px solid var(--interactive-primary);
  outline-offset: 2px;
}

.folder-item.active {
  background: rgba(16, 185, 129, 0.08);
  border-left-color: var(--p-primary-color, #10b981);
}

.folder-item.active .folder-name {
  color: var(--p-primary-color, #10b981);
  font-weight: 500;
}

.folder-item.drop-target {
  background: rgba(16, 185, 129, 0.18);
  outline: 1px dashed var(--p-primary-color, #10b981);
  outline-offset: -2px;
}

.folder-toggle,
.folder-toggle-spacer {
  flex: 0 0 auto;
  width: 1.1rem;
  height: 1.1rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.folder-toggle {
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  color: var(--p-text-muted-color, #94a3b8);
  border-radius: 3px;
  font-size: 0.7rem;
}

.folder-toggle:hover {
  color: var(--p-text-color);
  background: var(--p-content-hover-background, rgba(255, 255, 255, 0.06));
}

.folder-icon {
  flex: 0 0 auto;
  color: var(--p-primary-color, #10b981);
  opacity: 0.85;
  font-size: 0.95rem;
}

.folder-name {
  flex: 1 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.folder-count {
  flex: 0 0 auto;
  font-size: 0.7rem;
  font-weight: 500;
  color: var(--p-text-muted-color, #94a3b8);
  background: var(--p-content-hover-background, rgba(255, 255, 255, 0.06));
  padding: 0.1rem 0.4rem;
  border-radius: 10px;
  min-width: 1.4rem;
  text-align: center;
}

.folder-item.active .folder-count {
  color: var(--p-primary-color, #10b981);
  background: rgba(16, 185, 129, 0.18);
}

.folder-delete-btn {
  flex: 0 0 auto;
  background: none;
  border: none;
  padding: 0.15rem 0.3rem;
  cursor: pointer;
  color: var(--p-text-muted-color, #94a3b8);
  border-radius: 3px;
  opacity: 0;
  transition: opacity 0.12s ease;
  font-size: 0.75rem;
}

.folder-item:hover .folder-delete-btn,
.folder-delete-btn:focus-visible {
  opacity: 1;
}

.folder-delete-btn:hover {
  color: var(--p-red-500, #ef4444);
  background: var(--p-red-50, rgba(239, 68, 68, 0.08));
}

.folder-children {
  position: relative;
  /* Indent guide line */
  border-left: 1px solid var(--p-content-border-color, rgba(255, 255, 255, 0.08));
  margin-left: calc(0.4rem + 0.55rem);
}
</style>
