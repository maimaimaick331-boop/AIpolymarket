import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        dashboard: {
          bg: '#111827',
          card: '#1f2937',
          line: '#374151',
          good: '#4ade80',
          bad: '#f87171',
          muted: '#9ca3af',
          text: '#e5e7eb',
        },
      },
      keyframes: {
        pulseRing: {
          '0%': { boxShadow: '0 0 0 0 rgba(74, 222, 128, 0.8)' },
          '70%': { boxShadow: '0 0 0 8px rgba(74, 222, 128, 0)' },
          '100%': { boxShadow: '0 0 0 0 rgba(74, 222, 128, 0)' },
        },
      },
      animation: {
        pulseRing: 'pulseRing 1.6s ease-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config;
