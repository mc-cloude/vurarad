<template>
  <div class="w-full h-screen bg-deep flex flex-col">
    <!-- Tactical Header -->
    <header class="h-16 border-b border-gray-800 flex items-center justify-between px-6 bg-panel backdrop-blur-md z-20">
        <div class="flex items-center gap-4">
            <div class="w-2 h-2 rounded-full bg-success shadow-[0_0_8px_rgba(16,185,129,0.8)]"></div>
            <span class="font-mono text-sm text-muted tracking-widest">SYSTEM ONLINE</span>
        </div>
        <div class="font-mono text-cyan text-sm">
            [ VURA-RAD MISSION CONTROL ]
        </div>
    </header>

    <!-- Content Area -->
    <main class="flex-1 overflow-y-auto scrollbar-hide p-0">
       <div class="max-w-7xl mx-auto border-l border-r border-gray-800 min-h-full">
           <!-- List Header -->
           <div class="h-10 bg-gray-900/50 flex items-center px-14 border-b border-gray-800 font-mono text-xs text-muted uppercase tracking-wider">
               <div class="w-full grid grid-cols-12 gap-4">
                   <div class="col-span-1">Mod</div>
                   <div class="col-span-4">Patient Identity</div>
                   <div class="col-span-3">Timestamp</div>
                   <div class="col-span-2 text-right">AI Triage</div>
               </div>
           </div>

           <!-- Patient List -->
           <div class="divide-y divide-gray-800/50">
               <!-- Skeleton Loading State -->
               <template v-if="loading">
                   <SkeletonLoader v-for="i in 5" :key="i" />
               </template>

               <!-- Actual Data -->
               <template v-else>
                   <PatientStrip 
                     v-for="p in patients" 
                     :key="p.id" 
                     :patient="p" 
                     @click="openViewer(p.id)"
                   />
               </template>
           </div>
       </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import PatientStrip from '../components/molecules/PatientStrip.vue';
import SkeletonLoader from '../components/atoms/SkeletonLoader.vue';

const router = useRouter();
const loading = ref(true);
const patients = ref([]);

// Mock Data (Phase 4 will move this to Store)
const mockPatients = [
    { id: 'STRESS_01', name: 'John Doe', modality: 'CT', date: '2024-10-24 09:00', critical: false, ai_confidence: 45 },
    { id: 'STRESS_02', name: 'Jane Smith', modality: 'XR', date: '2024-10-24 09:15', critical: true, ai_confidence: 99 }, // Critical
    { id: 'STRESS_03', name: 'Alex Kale', modality: 'MR', date: '2024-10-24 10:30', critical: false, ai_confidence: 12 },
    { id: 'STRESS_04', name: 'Sarah Connor', modality: 'CT', date: '2024-10-24 11:00', critical: false, ai_confidence: 76 },
    { id: 'STRESS_05', name: 'Bruce Wayne', modality: 'XR', date: '2024-10-24 11:45', critical: true, ai_confidence: 88 },
];

onMounted(() => {
    // Simulate Network Latency (1.5s) to show Skeleton
    setTimeout(() => {
        patients.value = mockPatients;
        loading.value = false;
    }, 1500);
});

const openViewer = (id) => {
    router.push(`/viewer/${id}`);
};
</script>
