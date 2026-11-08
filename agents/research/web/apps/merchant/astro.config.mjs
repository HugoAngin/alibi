// @ts-check
import { defineConfig, envField } from 'astro/config';

// A simulated online marketplace. Fully static: built to dist/ and served by
// a Cloudflare Worker with static assets only (wrangler.jsonc).
export default defineConfig({
  // Absolute URLs (the connect button's return address) are built from this.
  site: process.env.MERCHANT_ORIGIN ?? 'http://localhost:4322',
  build: { format: 'directory' },
  env: {
    schema: {
      PUBLIC_ALIBI_ORIGIN: envField.string({
        context: 'client',
        access: 'public',
        default: 'http://localhost:4321',
      }),
    },
  },
});
