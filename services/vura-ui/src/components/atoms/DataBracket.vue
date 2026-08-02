<template>
  <div class="relative p-1">
    <!-- Top Left Corner -->
    <div class="absolute top-0 left-0 w-3 h-3 border-t-2 border-l-2 border-cyan pointer-events-none"></div>
    <!-- Bottom Right Corner -->
    <div class="absolute bottom-0 right-0 w-3 h-3 border-b-2 border-r-2 border-cyan pointer-events-none"></div>
    
    <!-- Content Slot -->
    <slot></slot>
  </div>
</template>

<script setup>
// DataBracket.vue
// Pure CSS/HTML component for the "Viewfinder" aesthetic
</script>
