// @ts-check
import { defineConfig, envField, sessionDrivers } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';

// The whole demo on one Cloudflare Worker: Alibi (the landing page and Maya's
// app), the simulated bank console under /bank/ and the simulated marketplace
// under /shop/. Pages render per request, because the screens read the
// visitor's run of the recorded demo (or, in the local live demo, the backend).
export default defineConfig({
  output: 'server',
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
      // "1" (the default): answer every backend call from the recorded demo (@alibi/ui/sim), per
      // visitor, with no backend and no credentials. `pnpm demo` sets "0" for the local live demo.
      ALIBI_SIMULATION: envField.string({ context: 'server', access: 'secret', default: '1' }),
      // The local live demo's director (alibi/demo_director.py, http://127.0.0.1:8103), with its
      // bearer, server-side only. Unset: no live demo, screens as before.
      ALIBI_DEMO_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
      ALIBI_DEMO_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
      // "1": Maya's simulation dashboard (/simulate/) can run, approve and send, for the live demo.
      ALIBI_SIMULATE_ACTIONS: envField.string({
        context: 'server',
        access: 'secret',
        optional: true,
      }),
      // The Python backend (web/docs/backend-integration.md). Unset means
      // "not connected": screens show the built-in case 1 data and say so.
      // `secret` access makes Astro read it at runtime (the Worker's vars),
      // instead of inlining whatever the build machine had.
      ALIBI_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
      // The backend's holder bearer (alibi/app/UI.md Credentials). A Worker secret, the same value
      // as the backend's ALIBI_HOLDER_TOKEN; forwarded from the server only (src/lib/screens.ts).
      ALIBI_HOLDER_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
      // The bank console (/bank/), local live demo only: the mock bank service (e.g.
      // http://127.0.0.1:8102), Bank A's bearer for it, and the analyst sign-in (the passcode and
      // the key that signs the session cookie). Unused by the simulation.
      BANK_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
      BANK_A_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
      ANALYST_PASSCODE: envField.string({ context: 'server', access: 'secret', optional: true }),
      BANK_SESSION_SECRET: envField.string({ context: 'server', access: 'secret', optional: true }),
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
