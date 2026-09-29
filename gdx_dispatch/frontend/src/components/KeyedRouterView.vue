<!--
  The app's routed-view outlet. Identical to <router-view> except the routed
  component is keyed so it REMOUNTS when navigation moves the same view to a
  different record (/estimates/A -> /estimates/B). Detail views load once in
  onMounted; without this the old record stayed on screen while every action
  hit the new one. The rule lives in lib/viewRemount.js.
-->
<template>
  <router-view v-slot="{ Component }">
    <component :is="Component" :key="viewEpoch" />
  </router-view>
</template>

<script setup>
import { useRouter } from 'vue-router';
import { installViewRemount } from '../lib/viewRemount';

const viewEpoch = installViewRemount(useRouter());
</script>
