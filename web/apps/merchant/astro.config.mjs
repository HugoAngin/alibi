// @ts-check
import { defineConfig, envField, sessionDrivers } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';

// A simulated online marketplace, on a Cloudflare Worker. Pages render per
// request so the connect flow can call the backend's merchant service
// (ALIBI_API_ORIGIN, path prefix /merchant) from the server.
export default defineConfig({
  // Absolute URLs (the connect button's return address) are built from this.
  site: process.env.MERCHANT_ORIGIN ?? 'http://localhost:4322',
  output: 'server',
  adapter: cloudflare({
    // No debugger port: the three apps run side by side (pnpm demo / pnpm dev) and would collide on 9229.
    inspectorPort: false,
    imageService: 'passthrough',
  }),
  // Sessions are unused; an in-memory driver stops the adapter provisioning KV.
  session: { driver: sessionDrivers.lruCache() },
  build: { format: 'directory' },
  // Screens are captured for demos and the deck; keep Astro's dev overlay out of them.
  devToolbar: { enabled: false },
  env: {
    schema: {
      // The live demo's director (alibi/demo_director.py): local http://127.0.0.1:8103, hosted the
      // Modal address. With its bearer, server-side only. Unset: no live demo, screens as before.
      ALIBI_DEMO_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
      ALIBI_DEMO_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
      PUBLIC_ALIBI_ORIGIN: envField.string({
        context: 'client',
        access: 'public',
        default: 'http://localhost:4321',
      }),
      // Read at runtime from the Worker's vars (`secret` access avoids inlining).
      ALIBI_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
    },
  },
});
