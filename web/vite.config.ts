import { readFileSync } from 'node:fs';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const frontendPort = 7987;
const versionFile = new URL('../src/syvrio/__init__.py', import.meta.url);
const version = readFileSync(versionFile, 'utf8').match(/^__version__ = "([^"]+)"$/m)?.[1];
if (!version) throw new Error('Could not read the Syvrio package version.');

export default defineConfig({
  plugins: [react()],
  define: { 'import.meta.env.VITE_SYVRIO_VERSION': JSON.stringify(version) },
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
