<template>
  <div class="fixed inset-y-0 right-0 w-1/3 bg-panel backdrop-blur-xl border-l border-white/10 transform transition-transform duration-300 z-40 flex flex-col shadow-2xl">
    <!-- Header -->
    <div class="h-16 border-b border-white/10 flex items-center justify-between px-6">
        <h2 class="font-sans font-bold text-lg tracking-wide text-white">DEBRIEF / REPORT</h2>
        <button class="text-muted hover:text-white font-mono">[CLOSE]</button>
    </div>

    <!-- Body (Editor) -->
    <div class="flex-1 p-6 space-y-6 overflow-y-auto">
        <!-- Findings List -->
        <div class="space-y-2">
            <label class="block text-xs font-mono text-muted uppercase">Confirmed Findings</label>
            <div class="p-3 bg-black/50 border border-white/10 text-sm font-mono text-cyan">
                > PNEUMOTHORAX (L) - CONFIRMED<br>
                > RIB FRACTURE (R6) - CONFIRMED
            </div>
        </div>

        <!-- Impression Text -->
        <div class="space-y-2">
            <label class="block text-xs font-mono text-muted uppercase">Impression</label>
            <textarea 
                class="w-full h-40 bg-black/30 border border-white/10 p-4 text-white font-sans text-sm resize-none focus:border-cyan outline-none"
                placeholder="Dictate or type impression..."
            >Left-sided pneumothorax with associated 6th rib fracture. Recommendation: Urgent chest tube placement.</textarea>
        </div>
    </div>

    <!-- Footer (The Payoff) -->
    <div class="p-6 border-t border-white/10 bg-black/40">
        <TacticalButton variant="success" class="w-full h-16 text-lg font-bold" @click="signAndBill">
            [ SIGN & BILL (KES 500) ]
        </TacticalButton>
    </div>
  </div>
</template>

<script setup>
import TacticalButton from '../components/atoms/TacticalButton.vue';

const signAndBill = () => {
    // Phase 4: Emit finalize event and play sound
    console.log('[Finance] Billing KES 500...');
    console.log('[Audio] Playing: Cha-Ching');
    alert("Report Finalized. Bill Sent.");
};
</script>
