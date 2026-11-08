/**
 * /act/ must never be an open proxy for the Bank A credential (eval, 04:17Z). Each attack below must
 * make no upstream call; the only call allowed is a same-origin, signed-in, known action whose path
 * the server built.
 */
import { describe, expect, it } from 'vitest';

import { handleAct, planAction, type Forward } from '../src/lib/actions';
import { bankTarget } from '../src/lib/bankApi';
import { backendHeaders } from '../src/lib/screens';
import { makeSession, verifySession } from '../src/lib/session';

const SITE = 'https://bank.example.test';
const ORIGIN = 'https://backend.example.test';

type Call = Parameters<Forward>[0];

function recorder() {
  const calls: Call[] = [];
  const forward: Forward = async (opts) => {
    calls.push(opts);
    return { kind: 'ok', data: null };
  };
  return { calls, forward };
}

const decision = {
  kind: 'decision',
  package_id: 'pkg-1',
  disposition: 'request_more',
  note: 'Need the settlement record',
};

async function act(
  fields: Record<string, string>,
  opts: { origin?: string | null; analyst?: string | null } = {},
) {
  const rec = recorder();
  const outcome = await handleAct(
    {
      fields,
      requestOrigin: opts.origin === undefined ? SITE : opts.origin,
      siteOrigin: SITE,
      analyst: opts.analyst === undefined ? 'J. Okafor' : opts.analyst,
    },
    {
      target: bankTarget(ORIGIN, undefined),
      headers: backendHeaders('bank-a-token'),
      forward: rec.forward,
    },
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
      { kind: 'followup', package_id: '%2e%2e', question: 'more please' },
      { ...decision, note: 'x'.repeat(301) },
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
    const { outcome, calls } = await act({
      ...decision,
      action: '/merchant/admin/seed',
      analyst_name: 'Someone Else',
    });
    expect(outcome.kind).toBe('sent');
    expect(calls).toHaveLength(1);
    expect(calls[0]!.origin).toBe(ORIGIN);
    expect(calls[0]!.path).toBe('/packages/pkg-1/disposition');
    expect(calls[0]!.body).toEqual({
      decision: 'request_more',
      reason: 'Need the settlement record',
    });
    expect(calls[0]!.headers).toEqual({ Authorization: 'Bearer bank-a-token' });
  });

  it('sends a follow-up as request_more with the question as requested evidence', async () => {
    const { calls } = await act({
      kind: 'followup',
      package_id: 'pkg-1',
      question: 'Settlement record please',
    });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.path).toBe('/packages/pkg-1/disposition');
    expect(calls[0]!.body).toEqual({
      decision: 'request_more',
      reason: 'Follow-up requested: Settlement record please',
      requested_evidence: ['Settlement record please'],
    });
  });

  it('goes through the public router prefix when no direct bank origin is set', () => {
    expect(
      planAction(decision, bankTarget(undefined, 'https://router.example.test')),
    ).toMatchObject({
      ok: true,
      path: '/bank/packages/pkg-1/disposition',
    });
  });

  it('plans paths without trusting the form', () => {
    expect(planAction({ ...decision, action: '/x' })).toMatchObject({
      ok: true,
      path: '/packages/pkg-1/disposition',
    });
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
    expect(
      await verifySession(secret, `${encodeURIComponent('Admin')}.${exp}.${sig}`, now),
    ).toBeNull();
    expect(await verifySession(secret, `${name}.${Number(exp) + 1}.${sig}`, now)).toBeNull();
    expect(await verifySession('other-secret', token, now)).toBeNull();
    expect(await verifySession(undefined, token, now)).toBeNull();
    expect(await verifySession(secret, undefined, now)).toBeNull();
  });
});
