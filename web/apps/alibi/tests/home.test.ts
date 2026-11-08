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
  explain,
  headline,
  initials,
  nextDue,
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
  due = '2026-11-09T09:00:00Z',
) => ({
  request_id: id,
  audience,
  recipient,
  question_count: 2,
  received_at: at,
  expires_at: due,
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

  it('lists the soonest reply-by date first among waiting requests', () => {
    const case1 = home({
      requests: [
        request('rfc-b', 'bank_b', 'Bank B', 'new', '2026-11-04T14:00:00Z', '2026-11-11T09:00:00Z'),
        request('rfc-a', 'bank_a', 'Bank A', 'new', '2026-11-02T09:00:00Z', '2026-11-09T09:00:00Z'),
        request(
          'rfc-s',
          'bank_a',
          'Bank A',
          'sent',
          '2026-11-05T09:00:00Z',
          '2026-11-01T09:00:00Z',
        ),
      ],
    });
    const items = activity(case1);
    expect(items.map((i) => i.id)).toEqual(['rfc-a', 'rfc-b', 'rfc-s']);
    expect(items[0]!.due).toBe('2026-11-09T09:00:00Z');
    expect(nextDue(case1)).toEqual({ recipient: 'Bank A', expires_at: '2026-11-09T09:00:00Z' });
  });

  it('has no next due date when nothing waits on her', () => {
    expect(nextDue(home())).toBeNull();
    const done = home({
      requests: [request('r1', 'bank_a', 'Bank A', 'sent', '2026-11-02T09:00:00Z')],
    });
    expect(nextDue(done)).toBeNull();
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
      lines: [
        {
          label: 'Sales',
          kind: 'order',
          sign: '+',
          amount: gbp(240000),
          source_ids: ['rec-0001-001 r1', 'rec-0001-002 r1'],
        },
        {
          label: 'Refunds',
          kind: 'refund',
          sign: '-',
          amount: gbp(18000),
          source_ids: ['rec-0001-010 r1'],
        },
        {
          label: 'Fees',
          kind: 'fee',
          sign: '-',
          amount: gbp(6000),
          source_ids: ['rec-0001-011 r1'],
        },
        {
          label: 'Payout',
          kind: 'payout',
          sign: '-',
          amount: gbp(216000),
          source_ids: ['rec-0001-021 r1'],
        },
      ],
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
    expect(entries[0]!.calculation).toMatchObject({
      expression: '240000 - 18000 - 6000 = 216000 (pence)',
      matches: true,
    });
  });

  it('types each reconciliation line and drops malformed ones', () => {
    const lines = entries[0]!.calculation!.lines;
    expect(lines.map((l) => [l.label, l.sign, l.amount.minor, l.source_ids.length])).toEqual([
      ['Sales', '+', 240000, 2],
      ['Refunds', '-', 18000, 1],
      ['Fees', '-', 6000, 1],
      ['Payout', '-', 216000, 1],
    ]);
    const broken = contextTimeline(
      data,
      new Map([
        [
          'txn-0001-004',
          { ...detail, reconciliation: { ...detail.reconciliation, lines: [1, 2] as never } },
        ],
      ]),
    );
    expect(broken[0]!.calculation).toBeNull();
  });

  group('explain', () => {
    it('cites the matching payout and how many records it covers', () => {
      expect(explain(entries[0]!)).toBe(
        "Matches Mock Marketplace's payout PO-0001-01, which settled on 27 Oct. The 4 records it covers add up to this amount.",
      );
    });

    it('says only that the amount matches when there is no reconciliation', () => {
      const plain = { ...entries[0]!, calculation: null };
      expect(explain(plain)).toBe(
        "Matches Mock Marketplace's payout of the same amount on 27 Oct.",
      );
    });

    it('says when linked details are missing', () => {
      expect(explain({ ...entries[0]!, explainedBy: null })).toBe(
        'Linked evidence details are unavailable here.',
      );
    });

    it('explains ambiguous and unmatched transactions without picking a record', () => {
      expect(explain({ ...entries[0]!, linkState: 'ambiguous' })).toBe(
        'More than one record could explain this. Alibi has not picked one.',
      );
      expect(explain(entries[1]!)).toBe(
        'Only your bank has a record of this. No connected merchant has sent anything that explains it yet.',
      );
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
