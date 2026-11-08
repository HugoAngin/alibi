/**
 * /act/ must never be an open proxy for the Bank A credential (eval, 04:17Z). Each attack below must
 * make no upstream call; the only call allowed is a same-origin, signed-in, known action whose path
 * the server built.
 */
import { describe, expect, it } from 'vitest';

import { handleAct, planAction, type Forward } from '../src/lib/actions';
import { backendHeaders } from '../src/lib/screens';
import { makeSession, verifySession } from '../src/lib/session';

const SITE = 'https://bank.example.test';
const ORIGIN = 'https://backend.example.test';

type Call = { origin: string | undefined; path: string; body: Record<string, string>; headers: Record<string, string> };

function recorder() {
  const calls: Call[] = [];
  const forward: Forward = async (opts) => {
    calls.push(opts);
    return { kind: 'ok', data: null };
  };
  return { calls, forward };
}

const decision = { kind: 'decision', package_id: 'pkg-1', disposition: 'request_more', note: 'Need the settlement record' };

async function act(fields: Record<string, string>, opts: { origin?: string | null; analyst?: string | null } = {}) {
  const rec = recorder();
  const outcome = await handleAct(
    {
      fields,
      requestOrigin: opts.origin === undefined ? SITE : opts.origin,
      siteOrigin: SITE,
      analyst: opts.analyst === undefined ? 'J. Okafor' : opts.analyst,
    },
    { origin: ORIGIN, headers: backendHeaders('bank-a-token'), forward: rec.forward },
  );
  return { outcome, calls: rec.calls };
}

describe('no upstream call for attacks', () => {
  it('ignores a client-supplied backend path (the reported exploit)', async () => {
    const { outcome, calls } = await act({ action: '/merchant/admin/seed', return_to: '/inbox/' });
    expect(outcome.kind).toBe('forbidden');
    expect(calls).toHaveLength(0);
  });

  it('refuses an unauthenticated request', async () => {
    const { outcome, calls } = await act(decision, { analyst: null });
    expect(outcome).toMatchObject({ kind: 'forbidden', status: 401 });
    expect(calls).toHaveLength(0);
  });

  it('refuses a cross-site or originless POST', async () => {
    for (const origin of ['https://evil.example', null]) {
      const { outcome, calls } = await act(decision, { origin });
      expect(outcome).toMatchObject({ kind: 'forbidden', status: 403 });
      expect(calls).toHaveLength(0);
    }
  });

  it('refuses path tricks in IDs and unknown kinds or decisions', async () => {
    const attempts: Record<string, string>[] = [
      { ...decision, package_id: '../../merchant/admin/seed' },
      { ...decision, package_id: 'pkg-1/../../x' },
      { ...decision, disposition: 'approve_everything' },
      { ...decision, note: '' },
      { kind: 'followup', parent_request_id: '%2e%2e', question: 'more please' },
      { kind: 'admin', package_id: 'pkg-1' },
    ];
    for (const fields of attempts) {
      const { outcome, calls } = await act(fields);
      expect(outcome.kind).toBe('forbidden');
      expect(calls).toHaveLength(0);
    }
  });
});

describe('the allowed actions', () => {
  it('sends a decision to the server-built Bank A path with only allowlisted fields', async () => {
    const { outcome, calls } = await act({ ...decision, action: '/merchant/admin/seed', analyst_name: 'Someone Else' });
    expect(outcome.kind).toBe('sent');
    expect(calls).toHaveLength(1);
    expect(calls[0]!.path).toBe('/bank/bank_a/packages/pkg-1/disposition');
    expect(calls[0]!.body).toEqual({ disposition: 'request_more', note: 'Need the settlement record' });
    expect(calls[0]!.headers).toEqual({ Authorization: 'Bearer bank-a-token' });
  });

  it('sends a follow-up that names only its parent and question', async () => {
    const { calls } = await act({ kind: 'followup', parent_request_id: 'rfc-0001-01', question: 'Settlement record please' });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.path).toBe('/bank/bank_a/requests');
    expect(calls[0]!.body).toEqual({
      parent_request_id: 'rfc-0001-01',
      question: 'Settlement record please',
    });
  });

  it('plans paths without trusting the form', () => {
    expect(planAction({ ...decision, action: '/x' })).toMatchObject({ ok: true, path: '/bank/bank_a/packages/pkg-1/disposition' });
  });
});

describe('analyst session', () => {
  const secret = 'test-session-secret';
  const now = Date.UTC(2026, 10, 8, 4, 30);

  it('round-trips the demo analyst, whose name contains a dot', async () => {
    const token = await makeSession(secret, 'J. Okafor', now);
    expect(token.split('.')).toHaveLength(3);
    expect(await verifySession(secret, token, now)).toBe('J. Okafor');
  });

  it('round-trips and expires', async () => {
    const token = await makeSession(secret, 'J. Okafor', now);
    expect(await verifySession(secret, token, now + 1000)).toBe('J. Okafor');
    expect(await verifySession(secret, token, now + 9 * 60 * 60 * 1000)).toBeNull();
  });

  it('rejects tampering, a wrong secret and a missing secret', async () => {
    const token = await makeSession(secret, 'J. Okafor', now);
    const [name, exp, sig] = token.split('.') as [string, string, string];
    expect(await verifySession(secret, `${encodeURIComponent('Admin')}.${exp}.${sig}`, now)).toBeNull();
    expect(await verifySession(secret, `${name}.${Number(exp) + 1}.${sig}`, now)).toBeNull();
    expect(await verifySession('other-secret', token, now)).toBeNull();
    expect(await verifySession(undefined, token, now)).toBeNull();
    expect(await verifySession(secret, undefined, now)).toBeNull();
  });
});
