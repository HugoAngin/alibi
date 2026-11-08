/**
 * "Request customer's Alibi" from the case panel: the form names only a case, the server picks that
 * case's request ID, and nothing goes upstream for a case without one. The case states come from
 * the bank's own inbox.
 */
import { describe, expect, it } from 'vitest';

import { cases, findCase } from '../src/data/cases';
import { handleAct, planAction, type Forward } from '../src/lib/actions';
import { bankTarget } from '../src/lib/bankApi';
import { alibiState, statusLabel } from '../src/lib/caseState';
import type { AnalystInbox } from '../src/lib/types';

const SITE = 'https://bank.example.test';
const target = bankTarget(undefined, 'https://backend.example.test');

function recorder() {
  const calls: Parameters<Forward>[0][] = [];
  const forward: Forward = async (opts) => {
    calls.push(opts);
    return { kind: 'ok', data: null };
  };
  return { calls, forward };
}

const maya = findCase('FC-0147')!;

describe('request action', () => {
  it("sends Maya's case request by ID to the bank's /rfc route", () => {
    const plan = planAction({ kind: 'request', case_id: 'FC-0147' }, target);
    expect(plan).toEqual({ ok: true, path: '/bank/rfc', body: { request_id: 'rfc-0001-01' } });
  });

  it('ignores a request ID or path supplied by the form', () => {
    const plan = planAction(
      { kind: 'request', case_id: 'FC-0147', request_id: 'rfc-0001-02', path: '/admin/seed' },
      target,
    );
    expect(plan).toEqual({ ok: true, path: '/bank/rfc', body: { request_id: 'rfc-0001-01' } });
  });

  it('refuses a case with no Alibi request, or an unknown case, before any upstream call', async () => {
    for (const case_id of ['FC-0152', 'FC-9999', '../admin/seed', '']) {
      const { calls, forward } = recorder();
      const out = await handleAct(
        {
          fields: { kind: 'request', case_id },
          requestOrigin: SITE,
          siteOrigin: SITE,
          analyst: 'J. Okafor',
        },
        { target, headers: {}, forward },
      );
      expect(out.kind).toBe('forbidden');
      expect(calls).toHaveLength(0);
    }
  });

  it('needs a signed-in analyst', async () => {
    const { calls, forward } = recorder();
    const out = await handleAct(
      {
        fields: { kind: 'request', case_id: 'FC-0147' },
        requestOrigin: SITE,
        siteOrigin: SITE,
        analyst: null,
      },
      { target, headers: {}, forward },
    );
    expect(out).toMatchObject({ kind: 'forbidden', status: 401 });
    expect(calls).toHaveLength(0);
  });
});

describe('case data', () => {
  it("carries case 1's Bank A request: ID, both questions and the reply deadline", () => {
    expect(maya.txn).toMatchObject({ id: 'txn-0001-004', amountMinor: 216000 });
    expect(maya.request?.id).toBe('rfc-0001-01');
    expect(maya.request?.questions.map((q) => q.id)).toEqual(['q.source_of_funds', 'q.breakdown']);
    expect(maya.request?.expiresAt).toBe('2026-11-09T09:00:00Z');
  });

  it('gives only case 1 a request; filler cases use txn-q- IDs', () => {
    for (const c of cases.filter((c) => c.id !== 'FC-0147')) {
      expect(c.request).toBeUndefined();
      expect(c.txn.id.startsWith('txn-q-')).toBe(true);
    }
  });
});

const inbox = (over: Partial<AnalystInbox>): AnalystInbox => ({
  recipient: { id: 'bank_a', name: 'Bank A', analyst_name: 'J. Okafor' },
  packages: [],
  requests_sent: [],
  ...over,
});

describe('alibiState', () => {
  it('starts at not requested in the preview', () => {
    expect(alibiState(maya, null)).toEqual({ kind: 'not_requested' });
    expect(statusLabel(maya, { kind: 'not_requested' })).toBe('Open');
  });

  it('shows a sent request as awaiting the customer', () => {
    const s = alibiState(
      maya,
      inbox({
        requests_sent: [
          {
            request_id: 'rfc-0001-01',
            parent_request_id: null,
            sent_at: '2026-11-02T09:00:00Z',
            status: 'awaiting_answer',
            question_count: 2,
          },
        ],
      }),
    );
    expect(s).toEqual({
      kind: 'requested',
      requestId: 'rfc-0001-01',
      sentAt: '2026-11-02T09:00:00Z',
    });
    expect(statusLabel(maya, s)).toBe('Awaiting customer');
  });

  it('links a received package to its review page', () => {
    const s = alibiState(
      maya,
      inbox({
        packages: [
          {
            package_id: 'pkg-1',
            request_id: 'rfc-0001-01',
            received_at: '2026-11-03T10:00:00Z',
            expires_at: '2026-11-09T09:00:00Z',
            subject_ref_masked: 'm***-001',
            authentication: 'verified',
            disposition: null,
            href: '/packages/pkg-1/',
          },
        ],
      }),
    );
    expect(s).toMatchObject({ kind: 'received', href: '/packages/pkg-1/' });
    expect(statusLabel(maya, s)).toBe('Alibi received');
  });

  it('never offers a request for a closed case or a case without one', () => {
    expect(alibiState(findCase('FC-0139')!, null).kind).toBe('closed');
    expect(alibiState(findCase('FC-0152')!, null).kind).toBe('unavailable');
  });
});
