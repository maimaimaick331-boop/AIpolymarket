import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  base: '/static/dashboard/',
  build: {
    outDir: path.resolve(__dirname, '../static/dashboard'),
    // Keep previous hashed assets so stale cached HTML does not hard-white-screen on rollout.
    emptyOutDir: false,
  },
});
