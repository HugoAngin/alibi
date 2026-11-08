/**
 * The phone app's Home logic (src/components/app/home.ts): request states, the activity list and
 * headline, banks paired with their feeds by audience, monograms, bank descriptions, and the
 * financial context timeline. Pure functions over the backend's JSON (MOBILE_NEEDS item 2).
 */
import { describe as group, expect, it } from 'vitest';

import {
  activity,
  alertTitle,
  banks,
  byMonth,
  contextTimeline,
  describe,
  headline,
  initials,
  plainName,
  requestState,
  type TxnDetail,
} from '../src/components/app/home';
import type { PassportHome } from '../src/lib/api/types';

const gbp = (minor: number) => ({ minor, currency: 'GBP' });

function home(over: Partial<PassportHome> = {}): PassportHome {
  return {
    holder: { name: 'Maya', subject_ref: 'maya-001' },
    connections: [
      {
        connection_id: 'conn-bank-a-maya-001',
        name: 'Bank A (mock)',
        kind: 'bank',
        status: 'connected',
        trust: 'bank_observed',
        last_synced_at: '2026-10-28T07:00:00Z',
        record_count: 12,
        href: '#',
      },
      {
        connection_id: 'conn-mock-marketplace',
        name: 'Mock Marketplace (mock)',
        kind: 'merchant',
        status: 'connected',
        trust: 'issuer_verified',
        last_synced_at: '2026-10-28T07:00:00Z',
        record_count: 20,
        href: '#',
      },
    ],
    transactions: [],
    evidence_status: {
      linked: 0,
      ambiguous: 0,
      unmatched: 0,
      total: 0,
      last_checked_at: '2026-10-28T07:00:00Z',
      superseded_sources: 0,
    },
    alerts: [],
    rules: [],
    requests: [],
    sharing_history: [],
    ...over,
  };
}

const request = (
  id: string,
  audience: string,
  recipient: string,
  status: PassportHome['requests'][number]['status'],
  at: string,
) => ({
  request_id: id,
  audience,
  recipient,
  question_count: 2,
  received_at: at,
  expires_at: '2026-11-09T09:00:00Z',
  status,
  href: '#',
});

group('requestState', () => {
  it('maps backend statuses to what is waiting on Maya', () => {
    expect(requestState('new')).toBe('awaiting');
    expect(requestState('investigating')).toBe('awaiting');
    expect(requestState('draft')).toBe('awaiting');
    expect(requestState('refused')).toBe('awaiting');
    expect(requestState('approved')).toBe('approved');
    expect(requestState('sent')).toBe('sent');
    expect(requestState('expired')).toBe('expired');
    expect(requestState('declined')).toBe('declined');
  });
});

group('activity and headline', () => {
  const data = home({
    requests: [
      request('rfc-0001-01', 'bank_a', 'Bank A', 'sent', '2026-11-02T09:00:00Z'),
      request('rfc-0001-02', 'bank_b', 'Bank B', 'new', '2026-11-04T14:00:00Z'),
    ],
    alerts: [
      {
        alert_id: 'al-1',
        kind: 'unexplained_credit',
        severity: 'warning',
        title: 'Unexplained credit of 216000 GBP',
        created_at: '2026-11-01T15:20:00Z',
        status: 'resolved',
        href: '#',
      },
    ],
  });

  it('puts what waits on Maya first, then newest first', () => {
    const items = activity(data);
    expect(items.map((i) => i.id)).toEqual(['rfc-0001-02', 'rfc-0001-01', 'al-1']);
    expect(items[0]).toMatchObject({
      label: 'Awaiting your review',
      href: '/app/requests/rfc-0001-02/',
    });
    expect(items[2]).toMatchObject({ title: 'Unexplained credit of £2,160.00', href: null });
  });

  it('writes minor units in alert titles as money', () => {
    expect(alertTitle('Unexplained credit of 216000 GBP')).toBe('Unexplained credit of £2,160.00');
    expect(alertTitle('No amount here')).toBe('No amount here');
  });

  it('names who is waiting, or what is ready to send', () => {
    expect(headline(data)).toBe('Bank B is waiting on your review.');
    const approved = home({
      requests: [request('r1', 'bank_a', 'Bank A', 'approved', '2026-11-02T09:00:00Z')],
    });
    expect(headline(approved)).toBe('Your approved package for Bank A is ready to send.');
    expect(headline(home())).toBe('Nothing is waiting on you.');
  });
});

group('banks', () => {
  it('pairs each recipient with its feed by audience and counts its requests and packages', () => {
    const data = home({
      requests: [
        request('rfc-0001-01', 'bank_a', 'Bank A', 'sent', '2026-11-02T09:00:00Z'),
        request('rfc-0001-02', 'bank_b', 'Bank B', 'new', '2026-11-04T14:00:00Z'),
      ],
      sharing_history: [
        {
          package_id: 'pkg-1',
          request_id: 'rfc-0001-01',
          audience: 'bank_a',
          recipient: 'Bank A',
          sent_at: '2026-11-03T10:00:00Z',
          expires_at: '2026-11-09T09:00:00Z',
          item_count: 20,
          status: 'delivered',
          href: '#',
        },
      ],
    });
    const [a, b] = banks(data);
    expect(a).toMatchObject({ audience: 'bank_a', name: 'Bank A', requests: 1, packagesSent: 1 });
    expect(a!.feed?.connection_id).toBe('conn-bank-a-maya-001');
    expect(b).toMatchObject({ audience: 'bank_b', awaiting: 1, feed: null });
  });

  it('never pairs bank_a with a bank_ab feed', () => {
    const data = home({
      connections: [{ ...home().connections[0]!, connection_id: 'conn-bank-ab-maya-001' }],
      requests: [request('r1', 'bank_a', 'Bank A', 'new', '2026-11-02T09:00:00Z')],
    });
    const list = banks(data);
    expect(list.find((x) => x.audience === 'bank_a')?.feed).toBeNull();
    expect(list).toHaveLength(2);
  });
});

group('names', () => {
  it('drops the backend’s mock marker and makes monograms', () => {
    expect(plainName('Mock Marketplace (mock)')).toBe('Mock Marketplace');
    expect(initials('Mock Marketplace (mock)')).toBe('MM');
    expect(initials('Bank A')).toBe('A');
  });

  it('turns a bank description into a title and a quieter line', () => {
    expect(describe('CARD (Synthetic Grocer)')).toEqual({
      title: 'Synthetic Grocer',
      line: 'Card',
    });
    expect(describe('MOCK MARKETPLACE PAYOUT PO-0001-01')).toEqual({
      title: 'Mock marketplace payout PO-0001-01',
      line: '',
    });
  });
});

group('contextTimeline', () => {
  const data = home({
    requests: [request('rfc-0001-01', 'bank_a', 'Bank A', 'sent', '2026-11-02T09:00:00Z')],
    transactions: [
      {
        txn_id: 'txn-0001-003',
        revision: 1,
        booked_at: '2026-10-20T10:00:00Z',
        description: 'CARD (Synthetic Grocer)',
        amount: gbp(-2450),
        direction: 'debit',
        link_state: 'unmatched',
        linked_count: 0,
        href: '#',
      },
      {
        txn_id: 'txn-0001-004',
        revision: 1,
        booked_at: '2026-10-28T06:30:00Z',
        description: 'MOCK MARKETPLACE PAYOUT PO-0001-01',
        amount: gbp(216000),
        direction: 'credit',
        link_state: 'linked',
        linked_count: 2,
        href: '#',
      },
    ],
  });
  const detail: TxnDetail = {
    txn: {
      txn_id: 'txn-0001-004',
      connection_name: 'Bank A (mock)',
      trust: 'bank_observed',
      references: ['PO-0001-01'],
    },
    sources: [
      {
        record_id: 'rec-0001-020',
        revision: 1,
        kind: 'balance_release',
        issuer: 'Mock Marketplace',
        trust: 'issuer_verified',
        occurred_at: '2026-10-25T00:00:00Z',
        amount: gbp(216000),
        status: 'final',
        summary: 'Released to seller balance',
        via_lookup: false,
        superseded: false,
      },
      {
        record_id: 'rec-0001-021',
        revision: 1,
        kind: 'payout',
        issuer: 'Mock Marketplace',
        trust: 'issuer_verified',
        occurred_at: '2026-10-27T16:00:00Z',
        amount: gbp(216000),
        status: 'settled',
        summary: 'Payout PO-0001-01 settled to Bank A',
        via_lookup: true,
        superseded: false,
      },
    ],
    reconciliation: {
      expression: '240000 - 18000 - 6000 = 216000',
      matches: true,
      lines: [1, 2, 3],
    },
    claims: [
      {
        claim_id: 'c1',
        question: 'Source of funds',
        status: 'supported',
        request_id: 'rfc-0001-01',
      },
      { claim_id: 'c0', question: 'Old', status: 'unresolved', state: 'invalidated' },
    ],
  };
  const entries = contextTimeline(data, new Map([['txn-0001-004', detail]]));

  it('lists transactions newest first', () => {
    expect(entries.map((e) => e.id)).toEqual(['txn-0001-004', 'txn-0001-003']);
  });

  it('explains a credit with the payout of the same amount, not the first record listed', () => {
    expect(entries[0]!.context).toBe('Payout PO-0001-01 settled to Bank A');
    expect(entries[0]!.observedBy).toBe('Bank A');
    expect(entries[0]!.calculation).toEqual({
      expression: '240000 - 18000 - 6000 = 216000 (pence)',
      matches: true,
    });
  });

  it('keeps only active claims, each with the bank it answered', () => {
    expect(entries[0]!.claims).toEqual([
      expect.objectContaining({ claim_id: 'c1', bank: 'Bank A' }),
    ]);
  });

  it('says so when nothing explains a transaction', () => {
    expect(entries[1]!.context).toBe('No record from your connections explains this yet.');
    expect(entries[1]!.calculation).toBeNull();
  });

  it('groups entries under month headings in order', () => {
    expect(byMonth(entries).map((g) => g.month)).toEqual(['October 2026']);
    expect(byMonth([{ at: 'n/a' }])[0]!.month).toBe('Earlier');
  });
});
