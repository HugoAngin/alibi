// @ts-check
import { defineConfig } from 'astro/config';

// A simulated bank's internal fraud-operations console. Fully static: built
// to dist/ and served by a Cloudflare Worker with static assets only.
export default defineConfig({
  site: process.env.BANK_ORIGIN ?? 'http://localhost:4323',
  build: { format: 'directory' },
});
