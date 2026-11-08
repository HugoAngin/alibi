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
      // Read at runtime from the Worker's vars (`secret` access avoids inlining).
      ALIBI_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
      // Bank A's credential for the backend, forwarded as a bearer token from the server only.
      BANK_A_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
      // The bank service directly (e.g. http://127.0.0.1:8102); unset means ALIBI_API_ORIGIN + /bank.
      BANK_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
      // Analyst sign-in: the demo passcode and the key that signs the session cookie.
      ANALYST_PASSCODE: envField.string({ context: 'server', access: 'secret', optional: true }),
      BANK_SESSION_SECRET: envField.string({ context: 'server', access: 'secret', optional: true }),
    },
  },
});
