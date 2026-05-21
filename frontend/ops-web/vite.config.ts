import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev server proxies API calls to the Django backend so the SPA is same-origin
// in dev. This keeps the session cookie + CSRF token handling clean: relative
// fetches go through Vite, which forwards them to localhost:8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/ops': 'http://localhost:8000',
      '/api': 'http://localhost:8000',
    },
  },
});
