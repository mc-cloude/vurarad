<template>
  <div 
    class="group relative h-20 w-full bg-deep border-b border-gray-800 flex items-center cursor-pointer hover:bg-white/5 transition-colors"
  >
    <!-- Hover Brackets (Only visible on hover) -->
    <div class="absolute inset-1 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
        <div class="absolute top-0 left-0 w-2 h-2 border-t border-l border-cyan"></div>
        <div class="absolute bottom-0 right-0 w-2 h-2 border-b border-r border-cyan"></div>
    </div>

    <!-- Bleed Bar (Left) -->
    <div 
        class="h-full w-1.5 mr-6 transition-all"
        :class="patient.critical ? 'bg-alert animate-pulse' : 'bg-muted group-hover:bg-cyan'"
    ></div>

    <!-- Data Columns -->
    <div class="flex-1 grid grid-cols-12 gap-4 items-center pr-6">
        <!-- Avatar/Status -->
        <div class="col-span-1 flex justify-center">
            <div class="w-10 h-10 rounded-none bg-gray-800 flex items-center justify-center font-mono text-xs text-muted">
                {{ patient.modality }}
            </div>
        </div>

        <!-- Name & ID -->
        <div class="col-span-4">
            <div class="text-white font-sans font-bold text-lg tracking-wide">{{ patient.name }}</div>
            <div class="text-muted font-mono text-sm tracking-widest">{{ patient.id }}</div>
        </div>

        <!-- Date -->
        <div class="col-span-3 text-muted font-mono text-sm">
            {{ patient.date }}
        </div>

        <!-- AI Status -->
        <div class="col-span-2 text-right">
             <span v-if="patient.ai_confidence > 80" class="text-cyan font-mono text-xs border border-cyan/30 px-2 py-1 bg-cyan/10">
                AI ANALYZED
             </span>
        </div>
        
        <!-- Action Arrow -->
        <div class="col-span-2 flex justify-end text-muted group-hover:text-cyan transition-colors">
            ->
        </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
    patient: {
        type: Object,
        required: true
    }
});
</script>
