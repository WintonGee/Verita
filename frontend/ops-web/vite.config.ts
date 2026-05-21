import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev server proxies API calls to the Django backend so the SPA is same-origin:
// keeps session cookie + CSRF handling clean. Target is env-configurable:
// localhost:8000 when run directly, http://backend:8000 inside docker compose.
const target = process.env.VITE_PROXY_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // listen on 0.0.0.0 so the container port is reachable
    port: 5174,
    proxy: {
      '/ops': { target, changeOrigin: true },
      '/api': { target, changeOrigin: true },
    },
  },
});
