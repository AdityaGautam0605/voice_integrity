import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync, existsSync } from 'node:fs';

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env };
  const cert = env.VIF_TLS_CERT || '.certs/demo.pem';
  const key = env.VIF_TLS_KEY || '.certs/demo-key.pem';
  const https = env.VIF_HTTP === '1' ? undefined : existsSync(cert) && existsSync(key)
    ? { cert: readFileSync(cert), key: readFileSync(key) } : undefined;
  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0', port: 5173, strictPort: true, https,
      fs: { deny: ['.env', '.env.*', '**/.git/**', '**/.certs/**', '**/keys/**', '**/data/**', '**/*.{pem,key,p12,crt}'] },
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8000', changeOrigin: true, ws: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
          configure: (proxy) => {
            // Keep the shared API token out of browser bundles.
            if (env.VIF_API_TOKEN) proxy.on('proxyReq', (request) => {
              request.setHeader('Authorization', `Bearer ${env.VIF_API_TOKEN}`);
            });
          },
        },
      },
    },
  };
});
