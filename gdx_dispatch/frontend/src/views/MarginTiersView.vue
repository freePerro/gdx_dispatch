<!--
  MarginTiersView — standalone wrapper around <MarginTiersPanel/>. The panel
  itself is also used as a tab in SettingsView. Keeping this route alive so
  existing deep links / tests still resolve.
-->
<template>
    <section class="margin-tiers-view view-card">
      <MarginTiersPanel />
    </section>
</template>

<script setup>
import MarginTiersPanel from '../components/MarginTiersPanel.vue';
</script>
