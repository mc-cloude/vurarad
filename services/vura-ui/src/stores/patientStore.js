import { defineStore } from 'pinia';
import { ref } from 'vue';

export const usePatientStore = defineStore('patients', () => {
    const patients = ref([]);

    const fetchPatients = () => {
        // Simulating API Call
        console.log('[Store] Fetching Patients...');
        patients.value = [
            { id: 'STRESS_01', name: 'John Doe', modality: 'CT', date: '2024-10-24 09:00', critical: false, ai_confidence: 45 },
            { id: 'STRESS_02', name: 'Jane Smith', modality: 'XR', date: '2024-10-24 09:15', critical: true, ai_confidence: 99 }, // Critical case
            { id: 'STRESS_03', name: 'Alex Kale', modality: 'MR', date: '2024-10-24 10:30', critical: false, ai_confidence: 12 },
            { id: 'STRESS_04', name: 'Sarah Connor', modality: 'CT', date: '2024-10-24 11:00', critical: false, ai_confidence: 76 },
            { id: 'STRESS_05', name: 'Bruce Wayne', modality: 'XR', date: '2024-10-24 11:45', critical: true, ai_confidence: 88 },
        ];
    };

    // Auto-fetch on init (optional, or call in View)
    fetchPatients();

    return { patients, fetchPatients };
});
