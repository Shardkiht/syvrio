import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Keep Vite/Vitest caches inside the project: some sandboxed environments
  // deny writes to the OS temp directory.
  cacheDir: 'node_modules/.vite',
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Vite output is copied into the Python package by scripts/build_web.py.
    assetsDir: 'assets',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    pool: 'threads',
    poolOptions: { threads: { singleThread: true } },
  },
});
