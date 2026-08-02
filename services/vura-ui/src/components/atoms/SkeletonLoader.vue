<template>
  <div class="w-full h-16 border-l-2 border-transparent bg-gray-900/30 animate-pulse flex items-center px-6 relative overflow-hidden group">
    <!-- Scanline Effect -->
    <div class="absolute inset-0 bg-gradient-to-r from-transparent via-cyan/5 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-1000"></div>

    <div class="w-full grid grid-cols-12 gap-4 items-center">
      <!-- Modality Stub -->
      <div class="col-span-1">
        <div class="h-4 w-8 bg-gray-800 rounded sm"></div>
      </div>
      
      <!-- Identity Stub -->
      <div class="col-span-4 flex flex-col gap-2">
         <div class="h-4 w-3/4 bg-gray-800 rounded sm"></div>
         <div class="h-3 w-1/2 bg-gray-800/50 rounded sm"></div>
      </div>

      <!-- Timestamp Stub -->
      <div class="col-span-3">
         <div class="h-3 w-24 bg-gray-800 rounded sm"></div>
      </div>

      <!-- AI Triage Stub -->
      <div class="col-span-2 col-start-11 flex justify-end">
         <div class="h-4 w-12 bg-gray-800 rounded sm"></div>
      </div>
    </div>
  </div>
</template>

<script setup>
// Pure presentation component
</script>

<style scoped>
/* Optional: Add a custom keyframe for a 'digital glitch' feel later */
</style>
