// @ts-check
import { defineConfig, sessionDrivers } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';

// The Alibi site: the product landing page and the app, on one Cloudflare
// Worker. Pages are prerendered by default; the few that read the request
// (`/`, which picks landing or app, and `/connect`) opt out with
// `export const prerender = false`.
export default defineConfig({
  site: process.env.ALIBI_ORIGIN ?? 'http://localhost:4321',
  adapter: cloudflare({
    // No image transforms yet, so no Cloudflare Images binding is provisioned.
    imageService: 'passthrough',
  }),
  // Astro sessions are unused. An in-memory driver stops the adapter from
  // provisioning a KV namespace on deploy; replace it when sessions are needed.
  session: { driver: sessionDrivers.lruCache() },
  build: { format: 'directory' },
  // Screens are captured for demos and the deck; keep Astro's dev overlay out of them.
  devToolbar: { enabled: false },
});
