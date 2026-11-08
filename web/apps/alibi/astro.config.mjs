// @ts-check
import clerk from '@clerk/astro';
import { defineConfig, envField, sessionDrivers } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';

// The Alibi site: the product landing page and the app, on one Cloudflare
// Worker. Every page renders per request, because Clerk (sign-up for Join)
// reads the session on each one.
export default defineConfig({
  output: 'server',
  integrations: [clerk()],
  site: process.env.ALIBI_ORIGIN ?? 'http://localhost:4321',
  adapter: cloudflare({
    // No debugger port: the three apps run side by side (pnpm demo / pnpm dev) and would collide on 9229.
    inspectorPort: false,
    // No image transforms yet, so no Cloudflare Images binding is provisioned.
    imageService: 'passthrough',
  }),
  // Astro sessions are unused. An in-memory driver stops the adapter from
  // provisioning a KV namespace on deploy; replace it when sessions are needed.
  session: { driver: sessionDrivers.lruCache() },
  build: { format: 'directory' },
  env: {
    schema: {
      // The Python backend (web/docs/backend-integration.md). Unset means
      // "not connected": screens show the built-in case 1 data and say so.
      // `secret` access makes Astro read it at runtime (the Worker's vars),
      // instead of inlining whatever the build machine had.
      ALIBI_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
      // Local demo only (`pnpm demo`, src/lib/demoHolder.ts): sign in as Maya without Clerk, on
      // 127.0.0.1 only. Never set these on the deployed Worker.
      ALIBI_DEMO_HOLDER: envField.string({ context: 'server', access: 'secret', optional: true }),
      HOLDER_SESSION_SECRET: envField.string({
        context: 'server',
        access: 'secret',
        optional: true,
      }),
    },
  },
  // Screens are captured for demos and the deck; keep Astro's dev overlay out of them.
  devToolbar: { enabled: false },
});
