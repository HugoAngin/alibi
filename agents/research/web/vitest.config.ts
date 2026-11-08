import { defineConfig } from 'vitest/config';

// One test run for the whole workspace. Tests cover the pure logic in each
// package's src/lib (or src/*.ts); components and pages are checked by
// `astro check` and the build instead.
export default defineConfig({
  test: {
    include: ['apps/*/tests/**/*.test.ts', 'packages/*/tests/**/*.test.ts'],
    environment: 'node',
  },
});
