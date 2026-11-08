/**
 * Maya's records from the team's canonical fixture, development case 1
 * (`agents/data/out/cases/case_0001.json`). SYNTHETIC. Identifiers, amounts
 * and times are copied from that file; tests/fixtures.test.ts fails if they
 * drift. Amounts are integer pence, times UTC.
 */

export const SUBJECT = 'maya-001';
export const MERCHANT = { id: 'mock-marketplace', name: 'Mock Marketplace' } as const;

export type RecordKind = 'sale' | 'fee' | 'refund' | 'purchase' | 'balance_release' | 'payout';

export interface MerchantRecord {
  id: string;
  kind: RecordKind;
  minor: number;
  occurredAt: string;
}

/** Pushed records 001–020 and the lookup-only payout 021, generated from the case file. */
export const merchantRecords: MerchantRecord[] = [
  { id: 'rec-0001-001', kind: 'sale', minor: 30000, occurredAt: '2026-10-05T10:00:00Z' },
  { id: 'rec-0001-002', kind: 'fee', minor: 750, occurredAt: '2026-10-05T10:01:00Z' },
  { id: 'rec-0001-003', kind: 'sale', minor: 25000, occurredAt: '2026-10-06T16:00:00Z' },
  { id: 'rec-0001-004', kind: 'fee', minor: 625, occurredAt: '2026-10-06T16:01:00Z' },
  { id: 'rec-0001-005', kind: 'refund', minor: 10000, occurredAt: '2026-10-07T12:00:00Z' },
  { id: 'rec-0001-006', kind: 'sale', minor: 40000, occurredAt: '2026-10-07T22:00:00Z' },
  { id: 'rec-0001-007', kind: 'fee', minor: 1000, occurredAt: '2026-10-07T22:01:00Z' },
  { id: 'rec-0001-008', kind: 'sale', minor: 35000, occurredAt: '2026-10-09T04:00:00Z' },
  { id: 'rec-0001-009', kind: 'fee', minor: 875, occurredAt: '2026-10-09T04:01:00Z' },
  { id: 'rec-0001-010', kind: 'sale', minor: 20000, occurredAt: '2026-10-10T10:00:00Z' },
  { id: 'rec-0001-011', kind: 'fee', minor: 500, occurredAt: '2026-10-10T10:01:00Z' },
  { id: 'rec-0001-012', kind: 'refund', minor: 8000, occurredAt: '2026-10-11T06:00:00Z' },
  { id: 'rec-0001-013', kind: 'sale', minor: 45000, occurredAt: '2026-10-11T16:00:00Z' },
  { id: 'rec-0001-014', kind: 'fee', minor: 1125, occurredAt: '2026-10-11T16:01:00Z' },
  { id: 'rec-0001-015', kind: 'sale', minor: 15000, occurredAt: '2026-10-12T22:00:00Z' },
  { id: 'rec-0001-016', kind: 'fee', minor: 375, occurredAt: '2026-10-12T22:01:00Z' },
  { id: 'rec-0001-017', kind: 'sale', minor: 30000, occurredAt: '2026-10-14T04:00:00Z' },
  { id: 'rec-0001-018', kind: 'fee', minor: 750, occurredAt: '2026-10-14T04:01:00Z' },
  { id: 'rec-0001-019', kind: 'purchase', minor: 4500, occurredAt: '2026-10-14T10:00:00Z' },
  {
    id: 'rec-0001-020',
    kind: 'balance_release',
    minor: 216000,
    occurredAt: '2026-10-26T08:00:00Z',
  },
  { id: 'rec-0001-021', kind: 'payout', minor: 216000, occurredAt: '2026-10-27T16:00:00Z' },
];

export interface BankRow {
  txnId: string;
  bookedAt: string;
  minor: number;
  description: string;
  counterparty: string;
}

export const bankRows: BankRow[] = [
  {
    txnId: 'txn-0001-001',
    bookedAt: '2026-10-06T19:00:00Z',
    minor: -65000,
    description: 'RENT',
    counterparty: 'Synthetic Lettings Ltd',
  },
  {
    txnId: 'txn-0001-002',
    bookedAt: '2026-10-08T23:00:00Z',
    minor: -1240,
    description: 'CARD',
    counterparty: 'Synthetic Grocer',
  },
  {
    txnId: 'txn-0001-003',
    bookedAt: '2026-10-10T21:00:00Z',
    minor: -2350,
    description: 'CARD',
    counterparty: 'Synthetic Pharmacy',
  },
  {
    txnId: 'txn-0001-004',
    bookedAt: '2026-10-28T06:30:00Z',
    minor: 216000,
    description: 'MOCK MARKETPLACE PAYOUT PO-0001-01',
    counterparty: 'Mock Marketplace Payments',
  },
];

export const CREDIT = bankRows[3];
export const PAYOUT = {
  recordId: 'rec-0001-021',
  reference: 'PO-0001-01',
  settledAt: '2026-10-27T16:00:00Z',
} as const;
export const RELEASE = { recordId: 'rec-0001-020', reference: 'REL-0001-01' } as const;

/** The records payout PO-0001-01 covers: 8 sales, 2 refunds, 8 fees. */
export const covered = merchantRecords.filter(
  (r) => r.kind === 'sale' || r.kind === 'refund' || r.kind === 'fee',
);

export function sum(kind: RecordKind): number {
  return covered.filter((r) => r.kind === kind).reduce((total, r) => total + r.minor, 0);
}

export const requests = {
  bankA: {
    requestId: 'rfc-0001-01',
    issuedAt: '2026-11-02T09:00:00Z',
    expiresAt: '2026-11-09T09:00:00Z',
    questions: [
      {
        question_id: 'q.source_of_funds',
        text: 'What is the origin of the incoming payment of £2,160.00 booked on 2026-10-28?',
      },
      { question_id: 'q.breakdown', text: 'Please show how this amount was calculated.' },
    ],
  },
  bankB: {
    requestId: 'rfc-0001-02',
    issuedAt: '2026-11-04T14:00:00Z',
    expiresAt: '2026-11-11T14:00:00Z',
    questions: [
      {
        question_id: 'q.sales_refunds_in_period',
        text: 'Please provide documented marketplace sales and refunds between 01 October 2026 and 31 October 2026.',
      },
    ],
  },
} as const;
