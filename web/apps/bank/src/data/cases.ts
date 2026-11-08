/**
 * The simulated bank's fraud-review queue. SYNTHETIC: fictional customers and
 * cases. The first case is Maya's £2,160 credit from the team's canonical
 * fixture (development case 1). The others only fill the queue: their IDs use
 * a `txn-q-` prefix so they can never be mistaken for data's case records,
 * and they carry no Alibi request, so the console cannot send one for them.
 */

export const bank = { id: 'bank_a', name: 'Bank A', unit: 'Financial Crime Operations' } as const;

export const analyst = { name: 'J. Okafor' } as const;

export type CaseStatus = 'open' | 'awaiting_customer' | 'closed';
export type Priority = 'high' | 'medium' | 'low';

/** The Alibi request a case can send: the bank service issues it by ID (POST /rfc). */
export interface CaseRequest {
  /** The bank service's request ID for this case, from data's case file. */
  id: string;
  questions: { id: string; text: string }[];
  /** The customer's reply window, as the request carries it. */
  expiresAt: string;
}

export interface FraudCase {
  id: string;
  customer: string;
  /** Masked: analysts see a reference, not the full account number. */
  accountRef: string;
  reason: string;
  priority: Priority;
  status: CaseStatus;
  openedAt: string;
  restriction: string;
  txn: { id: string; bookedAt: string; amountMinor: number; description: string };
  /** What the bank's own systems show about the transaction. */
  known: string[];
  /** What the bank cannot see, and so has to ask about. */
  unknown: string[];
  request?: CaseRequest;
}

export const cases: FraudCase[] = [
  {
    id: 'FC-0147',
    customer: 'Maya',
    accountRef: 'acct_****_001',
    reason: 'Unexplained incoming credit above usual activity',
    priority: 'high',
    status: 'open',
    openedAt: '2026-11-01T15:20:00Z',
    restriction: 'Outgoing payments paused pending review',
    txn: {
      id: 'txn-0001-004',
      bookedAt: '2026-10-28T06:30:00Z',
      amountMinor: 216000,
      description: 'MOCK MARKETPLACE PAYOUT PO-0001-01',
    },
    known: [
      'One credit, larger than this account usually receives',
      'Payer reference names a marketplace payout',
      'No earlier credits from this payer',
    ],
    unknown: ['Where the money came from', 'How the amount is made up'],
    // Bank A's request in data's case 1 (agents/data/out/cases/case_0001.json, rfc-0001-01).
    request: {
      id: 'rfc-0001-01',
      questions: [
        {
          id: 'q.source_of_funds',
          text: 'What is the origin of the incoming payment of £2,160.00 booked on 2026-10-28?',
        },
        { id: 'q.breakdown', text: 'Please show how this amount was calculated.' },
      ],
      expiresAt: '2026-11-09T09:00:00Z',
    },
  },
  {
    id: 'FC-0152',
    customer: 'D. Mensah',
    accountRef: 'acct_****_014',
    reason: 'Several credits from a new counterparty',
    priority: 'medium',
    status: 'open',
    openedAt: '2026-11-03T10:05:00Z',
    restriction: 'None',
    txn: {
      id: 'txn-q-0152',
      bookedAt: '2026-11-02T12:00:00Z',
      amountMinor: 84000,
      description: 'FASTER PAYMENT',
    },
    known: ['Four credits in six days', 'Payer first seen this month'],
    unknown: ['Relationship to the payer'],
  },
  {
    id: 'FC-0155',
    customer: 'S. Varga',
    accountRef: 'acct_****_031',
    reason: 'Credit followed by an immediate outgoing transfer',
    priority: 'high',
    status: 'open',
    openedAt: '2026-11-04T09:12:00Z',
    restriction: 'Outgoing payments paused pending review',
    txn: {
      id: 'txn-q-0155',
      bookedAt: '2026-11-04T08:41:00Z',
      amountMinor: 450000,
      description: 'FASTER PAYMENT',
    },
    known: ['Funds left the account within an hour', 'Receiving account opened recently'],
    unknown: ['Purpose of both payments'],
  },
  {
    id: 'FC-0144',
    customer: 'L. Brennan',
    accountRef: 'acct_****_022',
    reason: 'Cash deposits split across branches',
    priority: 'medium',
    status: 'awaiting_customer',
    openedAt: '2026-10-30T13:30:00Z',
    restriction: 'None',
    txn: {
      id: 'txn-q-0144',
      bookedAt: '2026-10-29T11:02:00Z',
      amountMinor: 95000,
      description: 'CASH DEPOSIT',
    },
    known: ['Three deposits on one day'],
    unknown: ['Source of the cash'],
  },
  {
    id: 'FC-0139',
    customer: 'R. Ilic',
    accountRef: 'acct_****_027',
    reason: 'Card-not-present purchase pattern',
    priority: 'low',
    status: 'closed',
    openedAt: '2026-10-25T08:40:00Z',
    restriction: 'None',
    txn: {
      id: 'txn-q-0139',
      bookedAt: '2026-10-24T19:12:00Z',
      amountMinor: -12999,
      description: 'CARD PURCHASE',
    },
    known: ['Confirmed by the customer by phone'],
    unknown: [],
  },
];

export function findCase(id: string | undefined): FraudCase | undefined {
  return cases.find((c) => c.id === id);
}

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

const dayFmt = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  timeZone: 'UTC',
});
const timeFmt = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'UTC',
});

/** "28 Oct" in UTC. */
export function day(iso: string): string {
  return dayFmt.format(new Date(iso));
}

/** "28 Oct, 06:30 UTC". */
export function when(iso: string): string {
  const d = new Date(iso);
  return `${dayFmt.format(d)}, ${timeFmt.format(d)} UTC`;
}
