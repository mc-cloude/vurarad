<template>
  <div class="relative w-full h-screen bg-black overflow-hidden select-none">
    
    <!-- 0. VolView Canvas Layer (The Engine) -->
    <!-- This ID is critical for the Wasm viewer to mount -->
    <div id="volview-root" class="absolute inset-0 z-0 bg-black flex items-center justify-center text-gray-800 font-mono">
        [ VOLVIEW RENDER CONTEXT INITIALIZING... ]
    </div>

    <!-- 1. HUD Layer (UI Overlay) -->
    <div class="absolute inset-0 z-10 pointer-events-none flex flex-col justify-between p-6">
        
        <!-- Top Bar -->
        <div class="flex justify-between items-start pointer-events-auto">
             <div class="glass-panel px-4 py-2 flex gap-4">
                 <button @click="$router.push('/worklist')" class="text-muted hover:text-white font-mono text-sm">&lt; BACK</button>
                 <span class="text-cyan font-bold tracking-wider">{{ $route.params.id }}</span>
             </div>
             
             <!-- AI Status -->
             <div class="glass-panel px-4 py-2 border-l-2 border-alert">
                 <span class="text-alert font-bold animate-pulse">CRITICAL FINDING: PNEUMTHX</span>
             </div>
        </div>

        <!-- Middle Zone -->
        <div class="flex-1 flex justify-between items-center px-2 py-8">
            <!-- Left Toolbar -->
            <div class="glass-panel w-12 flex flex-col gap-4 py-4 items-center pointer-events-auto">
                 <!-- Mock Tools -->
                 <div class="w-8 h-8 border border-white/20 hover:border-cyan hover:text-cyan flex items-center justify-center cursor-pointer transition-colors">+</div>
                 <div class="w-8 h-8 border border-white/20 hover:border-cyan hover:text-cyan flex items-center justify-center cursor-pointer transition-colors">-</div>
                 <div class="w-8 h-8 border border-white/20 hover:border-cyan hover:text-cyan flex items-center justify-center cursor-pointer transition-colors">o</div>
            </div>

            <!-- Right Panel (AI Findings) -->
            <div class="glass-panel w-64 h-2/3 flex flex-col pointer-events-auto">
                <div class="bg-white/5 p-3 text-xs font-mono text-muted uppercase tracking-widest border-b border-white/10">
                    AI Analysis
                </div>
                <div class="flex-1 p-4 space-y-4 overflow-y-auto">
                    <!-- Finding 1 -->
                    <div class="text-sm">
                        <span class="text-alert font-bold block mb-1">Pneumothorax (L)</span>
                        <span class="text-gray-400 text-xs">Confidence: 99.2%</span>
                    </div>
                    <!-- Finding 2 -->
                     <div class="text-sm">
                        <span class="text-white block mb-1">Rib Fracture (R6)</span>
                        <span class="text-gray-400 text-xs">Confidence: 84.1%</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Bottom Bar (Dictation) -->
        <div class="flex justify-center pointer-events-auto pb-4">
             <TacticalButton variant="cyan" class="shadow-[0_0_20px_rgba(0,229,255,0.2)]">
                [ PRESS TO DICTATE ]
             </TacticalButton>
        </div>

    </div>
  </div>
</template>

<script setup>
import TacticalButton from '../components/atoms/TacticalButton.vue';
</script>
