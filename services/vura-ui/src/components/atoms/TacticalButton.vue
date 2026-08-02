<template>
  <button 
    class="relative flex items-center justify-center px-6 transition-all duration-75 min-h-[44px] border font-mono tracking-wider uppercase group"
    :class="variantClasses"
    @click="playClickSound"
  >
    <!-- Glow Effect on Hover -->
    <div class="absolute inset-0 opacity-0 group-hover:opacity-20 transition-opacity duration-75" :class="bgClass"></div>
    
    <!-- Text Content -->
    <span class="relative z-10 flex items-center gap-2 group-hover:drop-shadow-[0_0_8px_rgba(0,229,255,0.8)]">
      <slot></slot>
    </span>
  </button>
</template>

<script setup>
import { computed } from 'vue';

const props = defineProps({
  variant: {
    type: String,
    default: 'cyan', // cyan, alert, success
    validator: (value) => ['cyan', 'alert', 'success'].includes(value)
  }
});

const variantClasses = computed(() => {
  switch (props.variant) {
    case 'alert':
      return 'border-alert text-alert hover:text-white';
    case 'success':
      return 'border-success text-success hover:text-white';
    default:
      return 'border-cyan text-cyan hover:text-white';
  }
});

const bgClass = computed(() => {
  switch (props.variant) {
    case 'alert': return 'bg-alert';
    case 'success': return 'bg-success';
    default: return 'bg-cyan';
  }
});

const playClickSound = () => {
    // Placeholder for Phase 4 Audio
    console.log('[Audio] Click');
};
</script>
