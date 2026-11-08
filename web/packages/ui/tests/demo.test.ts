/** The demo director client (src/demo.ts): configuration, shape checks, and refusals verbatim. */
import { describe, expect, it } from 'vitest';

import { demoState, demoStep, isDemoState, liveBody } from '../src/demo';

const target = { origin: 'http://127.0.0.1:8103', token: 't' };
const json = (status: number, body: unknown) =>
  (async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })) as unknown as typeof fetch;
const sample = {
  version: 'abc',
  stage: 'ready',
  as_of: '2026-10-25T00:00:00Z',
  steps: { withdrawn: false, flagged: false, requested: false, received: false },
  request_id: 'rfc-0001-01',
  package_id: null,
  credit_txn: 'txn-0001-004',
  flagged_at: '2026-10-29T08:00:00Z',
};

describe('demo client', () => {
  it('is off without an origin or token', async () => {
    expect(await demoState({ origin: undefined, token: 't' })).toBeNull();
    const r = await demoStep({ origin: target.origin, token: undefined }, 'reset');
    expect(r.kind).toBe('unavailable');
  });

  it('reads the state and exposes only version and stage to the browser', async () => {
    const s = await demoState(target, json(200, sample));
    expect(s && isDemoState(s)).toBe(true);
    expect(liveBody(s)).toEqual({ version: 'abc', stage: 'ready' });
    expect(liveBody(null)).toEqual({ version: null, stage: null });
  });

  it('treats a 401 or an odd shape as no demo', async () => {
    expect(await demoState(target, json(401, { code: 'not_authenticated' }))).toBeNull();
    expect(await demoState(target, json(200, { hello: 1 }))).toBeNull();
  });

  it('passes the director’s refusal through word for word', async () => {
    const refusal = { code: 'not_flagged_yet', reason: 'withdraw the payout first' };
    const r = await demoStep(target, 'request', { recipient: 'bank_a' }, json(409, refusal));
    expect(r).toEqual({ kind: 'refused', refusal });
  });
});
