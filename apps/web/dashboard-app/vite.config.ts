import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  base: '/static/dashboard/',
  build: {
    outDir: path.resolve(__dirname, '../static/dashboard'),
    emptyOutDir: true,
  },
});
