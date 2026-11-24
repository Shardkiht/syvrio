import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const frontendPort = 7987;

export default defineConfig({
  plugins: [react()],
  // 将 Vite 和 Vitest 缓存放在项目内，兼容无法写入操作系统临时目录的环境。
  cacheDir: 'node_modules/.vite',
  server: {
    host: '127.0.0.1',
    port: frontendPort,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8746', changeOrigin: false },
    },
  },
  preview: { port: frontendPort, strictPort: true },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // scripts/build_web.py 将 Vite 构建产物复制到 Python 包内。
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
