import { defineConfig } from 'vite';

// Assets remain local and keep the original Monaco worker's relative URLs.
export default defineConfig({
  clearScreen: false,
  server: { host: '127.0.0.1', port: 1420, strictPort: true },
  preview: { host: '127.0.0.1', port: 1420, strictPort: true },
  build: { target: ['es2022', 'chrome105', 'safari15'], sourcemap: true },
});
