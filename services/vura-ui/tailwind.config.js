/** @type {import('tailwindcss').Config} */
export default {
    content: [
        "./index.html",
        "./src/**/*.{vue,js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            fontFamily: {
                sans: ['"Manrope"', 'sans-serif'], // Primary UI
                mono: ['"JetBrains Mono"', 'monospace'], // Data/Numbers
            },
            colors: {
                // "Dark Room" Palette
                deep: '#0B0F19', // Vantablack
                panel: 'rgba(20, 25, 35, 0.85)', // Glass Panel
                cyan: '#00E5FF', // Vura Cyan (Action)
                alert: '#FF2A68', // Triage Red (Critical)
                success: '#10B981', // Emerald (Money)
                muted: '#64748B', // Tungsten (Secondary)
            },
            borderRadius: {
                DEFAULT: '0px', // FORCE SQUARE CORNERS
                'none': '0px',
            },
            cursor: {
                crosshair: 'crosshair', // Tactical feel
            },
            backgroundImage: {
                'grid-pattern': "linear-gradient(to right, #ffffff05 1px, transparent 1px), linear-gradient(to bottom, #ffffff05 1px, transparent 1px)",
                'scanline': "repeating-linear-gradient(0deg, transparent, transparent 1px, #000000 2px, #000000 3px)"
            }
        }
    },
    plugins: [],
}
