import { describe, expect, it } from 'vitest';

import {
  HOLDER_TTL_MS,
  demoHolderEnabled,
  isHolderRoute,
  isLoopback,
  makeHolderSession,
  safeNext,
  verifyHolderSession,
} from '../src/lib/demoHolder';

const SECRET = 'a'.repeat(64);

describe('demo holder mode', () => {
  it('turns on only with the flag and a long secret', () => {
    expect(demoHolderEnabled('1', SECRET)).toBe(true);
    expect(demoHolderEnabled('1', undefined)).toBe(false);
    expect(demoHolderEnabled('1', 'short')).toBe(false);
    expect(demoHolderEnabled(undefined, SECRET)).toBe(false);
    expect(demoHolderEnabled('true', SECRET)).toBe(false);
  });

  it('accepts loopback hosts only', () => {
    for (const h of ['127.0.0.1', 'localhost', '[::1]', '127.1.2.3'])
      expect(isLoopback(h)).toBe(true);
    for (const h of [
      'alibi-app.workers.dev',
      '0.0.0.0',
      '192.168.1.5',
      '127.0.0.1.nip.io',
      '1127.0.0.1',
    ])
      expect(isLoopback(h)).toBe(false);
  });

  it('gates every holder route', () => {
    for (const p of [
      '/app',
      '/app/',
      '/app/act',
      '/app/requests/rfc-0001-01/',
      '/phone/',
      '/connect',
    ])
      expect(isHolderRoute(p)).toBe(true);
    for (const p of ['/', '/simulate/', '/sign-in/', '/demo-sign-in/', '/application'])
      expect(isHolderRoute(p)).toBe(false);
  });

  it('only returns to same-site paths', () => {
    expect(safeNext('/app/requests/rfc-0001-01/')).toBe('/app/requests/rfc-0001-01/');
    for (const bad of [null, '', 'https://evil.example/', '//evil.example/', '/\\evil.example'])
      expect(safeNext(bad)).toBe('/app/');
  });

  it('issues sessions that verify, and refuses tampered, expired or foreign ones', async () => {
    const now = 1_000_000;
    const token = await makeHolderSession(SECRET, 'maya', now);
    expect(await verifyHolderSession(SECRET, token, now + 1)).toBe('maya');
    expect(await verifyHolderSession(SECRET, token, now + HOLDER_TTL_MS)).toBeNull();
    expect(await verifyHolderSession('b'.repeat(64), token, now + 1)).toBeNull();
    expect(await verifyHolderSession(SECRET, token.replace('maya', 'mayb'), now + 1)).toBeNull();
    expect(await verifyHolderSession(SECRET, `${token}x`, now + 1)).toBeNull();
    expect(await verifyHolderSession(undefined, token, now + 1)).toBeNull();
    expect(await verifyHolderSession(SECRET, undefined, now + 1)).toBeNull();
    await expect(makeHolderSession(SECRET, 'Maya.Patel', now)).rejects.toThrow();
  });
});
