import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': process.env.VITE_API_TARGET || 'http://127.0.0.1:18080' } },
  test: { environment: 'jsdom', setupFiles: './src/test-setup.js', css: false, include: ['src/**/*.{test,spec}.{js,jsx,ts,tsx}'], exclude: ['e2e/**'] },
});
