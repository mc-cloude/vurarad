<template>
  <div class="relative w-full h-screen flex items-center justify-center bg-deep bg-grid-pattern overflow-hidden">
    <!-- Scanline Overlay -->
    <ScanlineOverlay />
    
    <!-- 3D Skull Placeholder (Center) -->
    <div id="webgl-skull" class="absolute inset-0 flex items-center justify-center opacity-30 pointer-events-none">
       <!-- WebGL Canvas will mount here -->
       <div class="w-64 h-64 border border-muted rounded-full flex items-center justify-center text-muted">
         [ NEURAL LINK ]
       </div>
    </div>

    <!-- Auth Form (The Airlock Interface) -->
    <div class="glass-panel p-12 z-10 w-full max-w-md relative">
      <h1 class="text-2xl font-sans font-bold tracking-tight text-white mb-8 text-center flex items-center justify-center gap-2">
        <div class="w-3 h-3 bg-cyan animate-pulse"></div> VURA<span class="text-cyan">BOX</span> EDGE
      </h1>

      <form @submit.prevent="handleLogin" class="space-y-8">
        <div class="space-y-2">
          <label class="block text-xs font-mono text-muted uppercase tracking-widest">Operator ID</label>
          <input 
            v-model="username" 
            type="text" 
            class="w-full bg-transparent border-b border-muted focus:border-cyan outline-none py-2 text-white font-mono text-lg transition-colors placeholder-gray-700"
            placeholder="ID-XXXX"
            autofocus
          />
        </div>

        <div class="space-y-2">
          <label class="block text-xs font-mono text-muted uppercase tracking-widest">Passcode</label>
          <input 
            v-model="password" 
            type="password" 
            class="w-full bg-transparent border-b border-muted focus:border-cyan outline-none py-2 text-white font-mono text-lg transition-colors placeholder-gray-700"
            placeholder="••••••" 
          />
        </div>

        <div class="pt-6">
           <TacticalButton variant="cyan" class="w-full h-12">
             [ AUTHENTICATE ]
           </TacticalButton>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import TacticalButton from '../components/atoms/TacticalButton.vue';
import ScanlineOverlay from '../components/atoms/ScanlineOverlay.vue';

const router = useRouter();
const username = ref('');
const password = ref('');

const handleLogin = () => {
    // Simulate Auth
    console.log('[Auth] Authenticating...');
    setTimeout(() => {
        router.push('/worklist');
    }, 800);
};
</script>
