// @ts-check
import { defineConfig, envField, sessionDrivers } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';

// A simulated bank's internal fraud-operations console, on a Cloudflare
// Worker. Pages render per request so the console can call the backend
// (ALIBI_API_ORIGIN, path prefix /bank) from the server, with the bank's
// credential kept server-side. Pages that only show built-in data can set
// `export const prerender = true`.
export default defineConfig({
  site: process.env.BANK_ORIGIN ?? 'http://localhost:4323',
  output: 'server',
  adapter: cloudflare({ imageService: 'passthrough' }),
  // Sessions are unused; an in-memory driver stops the adapter provisioning KV.
  session: { driver: sessionDrivers.lruCache() },
  build: { format: 'directory' },
  // Screens are captured for demos and the deck; keep Astro's dev overlay out of them.
  devToolbar: { enabled: false },
  env: {
    schema: {
      // Read at runtime from the Worker's vars (`secret` access avoids inlining).
      ALIBI_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
    },
  },
});
