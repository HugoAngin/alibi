/**
 * The simulated bank's fraud-review queue. SYNTHETIC: fictional customers and
 * cases. The first case is Maya's £2,160 credit from the team's canonical
 * fixture (development case 1); the others fill the queue.
 */

export const bank = { id: 'bank_a', name: 'Bank A', unit: 'Financial Crime Operations' } as const;

export const analyst = { name: 'J. Okafor' } as const;

export type CaseStatus = 'open' | 'awaiting_customer' | 'closed';

export interface FraudCase {
  id: string;
  customer: string;
  /** Masked: analysts see a reference, not the full account number. */
  accountRef: string;
  reason: string;
  status: CaseStatus;
  openedAt: string;
  restriction: string;
  txn: { id: string; bookedAt: string; amountMinor: number; description: string };
}

export const cases: FraudCase[] = [
  {
    id: 'FC-0147',
    customer: 'Maya',
    accountRef: 'acct_****_001',
    reason: 'Unexplained incoming credit above usual activity',
    status: 'open',
    openedAt: '2026-11-01T15:20:00Z',
    restriction: 'Outgoing payments paused pending review',
    txn: {
      id: 'txn-0001-004',
      bookedAt: '2026-10-28T06:30:00Z',
      amountMinor: 216000,
      description: 'MOCK MARKETPLACE PAYOUT PO-0001-01',
    },
  },
  {
    id: 'FC-0152',
    customer: 'D. Mensah',
    accountRef: 'acct_****_014',
    reason: 'Several credits from a new counterparty',
    status: 'open',
    openedAt: '2026-11-03T10:05:00Z',
    restriction: 'None',
    txn: {
      id: 'txn-0014-002',
      bookedAt: '2026-11-02T12:00:00Z',
      amountMinor: 84000,
      description: 'FASTER PAYMENT',
    },
  },
  {
    id: 'FC-0139',
    customer: 'R. Ilic',
    accountRef: 'acct_****_027',
    reason: 'Card-not-present purchase pattern',
    status: 'closed',
    openedAt: '2026-10-25T08:40:00Z',
    restriction: 'None',
    txn: {
      id: 'txn-0027-009',
      bookedAt: '2026-10-24T19:12:00Z',
      amountMinor: -12999,
      description: 'CARD PURCHASE',
    },
  },
];

/** The questions an RFC can carry. IDs match shared/CONTRACTS.md. */
export const questions = [
  { id: 'q.source_of_funds', text: 'What is the source of this credit?' },
  { id: 'q.breakdown', text: 'How is the amount made up?' },
  {
    id: 'q.sales_refunds_in_period',
    text: 'What marketplace sales and refunds did the customer make in a period?',
  },
] as const;

const gbp = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' });

export function formatGBP(minor: number): string {
  return gbp.format(minor / 100);
}
