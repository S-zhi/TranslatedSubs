import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 云端试用只服务官网部署。默认开启；仅当构建者显式传入 '0' 时关闭，
// 其他任何取值都视为开启，避免拼写意外丢掉功能。关闭后整套试用模块
// 会被死代码消除，产物不含 /api/trials 客户端。
const cloudTrialEnabled = process.env.SUBTRANS_ENABLE_CLOUD_TRIAL !== '0';

export default defineConfig({
  plugins: [react()],
  base: './',
  define: { __CLOUD_TRIAL_ENABLED__: cloudTrialEnabled },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api/trials': {
        target: process.env.TRIAL_PROXY_TARGET || 'http://127.0.0.1:8001',
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
  },
});
