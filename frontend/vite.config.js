import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const API = process.env.SF_DEV_API || 'http://127.0.0.1:8300'

// Dev: the backend runs on 127.0.0.1:8300 (SF_COOKIE_SECURE=0).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // ws: the admin panel VNC (/portal/admin/vnc/ws) runs over WebSocket
      '/portal': { target: API, ws: true },
      '/v1': API,
    },
  },
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 600 },
})
