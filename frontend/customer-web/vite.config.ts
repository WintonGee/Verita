import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Proxy /v1 and /api to the backend so the SPA is same-origin in dev.
// This avoids CORS and httpOnly-cookie issues during local development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: 'http://localhost:8000', changeOrigin: true },
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
});
