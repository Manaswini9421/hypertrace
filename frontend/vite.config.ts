import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard talks to the API through this proxy in dev, so the browser
// only ever sees one origin (no CORS round-trips during local development).
// Point VITE_API_TARGET at wherever `kubectl port-forward svc/api-bff` lands.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/vitest.setup.ts'],
    globals: true,
    // Excludes the default '**/dist/**' plus node_modules; keeps runs fast
    // and stops a stale build from being collected as tests.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:18000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
