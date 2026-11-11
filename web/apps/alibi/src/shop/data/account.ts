/**
 * Maya's seller account as Mock Marketplace itself shows it. SYNTHETIC.
 * Copied from data's marketplace view of case 1,
 * `agents/data/out/merchant_view/case_0001.json` (built from
 * `agents/data/out/cases/case_0001.json`); tests/account.test.ts fails if it
 * drifts. Amounts are integer pence, times UTC.
 */

export interface Order {
  orderRef: string;
  title: string;
  soldAt: string;
  priceMinor: number;
  feeMinor: number;
  status: string;
  recordId: string;
}

export interface Refund {
  refundRef: string;
  orderRef: string;
  refundedAt: string;
  amountMinor: number;
  reason: string;
  recordId: string;
}

export interface Payout {
  payoutRef: string;
  requestedAt: string;
  amountMinor: number;
  status: string;
  settledAt: string;
  destination: string;
  coversOrderRefs: string[];
  recordId: string;
}

export interface Notification {
  sentAt: string;
  title: string;
  body: string;
  recordId: string;
}

export interface Purchase {
  boughtAt: string;
  title: string;
  amountMinor: number;
  recordId: string;
}

export const orders: Order[] = [
  {
    orderRef: 'MM-48001',
    title: 'Vintage denim jacket',
    soldAt: '2026-10-05T10:00:00Z',
    priceMinor: 30000,
    feeMinor: 750,
    status: 'Completed',
    recordId: 'rec-0001-001',
  },
  {
    orderRef: 'MM-48017',
    title: 'Wool coat, size 10',
    soldAt: '2026-10-06T16:00:00Z',
    priceMinor: 25000,
    feeMinor: 625,
    status: 'Completed',
    recordId: 'rec-0001-003',
  },
  {
    orderRef: 'MM-48042',
    title: 'Leather boots and two scarves',
    soldAt: '2026-10-07T22:00:00Z',
    priceMinor: 40000,
    feeMinor: 1000,
    status: 'Completed',
    recordId: 'rec-0001-006',
  },
  {
    orderRef: 'MM-48066',
    title: 'Cashmere jumper and silk scarf',
    soldAt: '2026-10-09T04:00:00Z',
    priceMinor: 35000,
    feeMinor: 875,
    status: 'Completed',
    recordId: 'rec-0001-008',
  },
  {
    orderRef: 'MM-48081',
    title: 'Pleated midi skirt',
    soldAt: '2026-10-10T10:00:00Z',
    priceMinor: 20000,
    feeMinor: 500,
    status: 'Completed',
    recordId: 'rec-0001-010',
  },
  {
    orderRef: 'MM-48107',
    title: 'Bundle: 5 summer dresses',
    soldAt: '2026-10-11T16:00:00Z',
    priceMinor: 45000,
    feeMinor: 1125,
    status: 'Completed',
    recordId: 'rec-0001-013',
  },
  {
    orderRef: 'MM-48122',
    title: 'Two cotton shirts',
    soldAt: '2026-10-12T22:00:00Z',
    priceMinor: 15000,
    feeMinor: 375,
    status: 'Completed',
    recordId: 'rec-0001-015',
  },
  {
    orderRef: 'MM-48139',
    title: 'Bundle: 3 winter knits',
    soldAt: '2026-10-14T04:00:00Z',
    priceMinor: 30000,
    feeMinor: 750,
    status: 'Completed',
    recordId: 'rec-0001-017',
  },
];

export const refunds: Refund[] = [
  {
    refundRef: 'MM-48017-R1',
    orderRef: 'MM-48017',
    refundedAt: '2026-10-07T12:00:00Z',
    amountMinor: 10000,
    reason: 'Button missing on arrival (partial refund)',
    recordId: 'rec-0001-005',
  },
  {
    refundRef: 'MM-48081-R1',
    orderRef: 'MM-48081',
    refundedAt: '2026-10-11T06:00:00Z',
    amountMinor: 8000,
    reason: 'Hem slightly damaged (partial refund)',
    recordId: 'rec-0001-012',
  },
];

export const payouts: Payout[] = [
  {
    payoutRef: 'PO-0001-01',
    requestedAt: '2026-10-26T09:00:00Z',
    amountMinor: 216000,
    status: 'Paid to bank',
    settledAt: '2026-10-27T16:00:00Z',
    destination: 'Bank account ending 00',
    coversOrderRefs: [
      'MM-48001',
      'MM-48017',
      'MM-48042',
      'MM-48066',
      'MM-48081',
      'MM-48107',
      'MM-48122',
      'MM-48139',
    ],
    recordId: 'rec-0001-021',
  },
];

export const notifications: Notification[] = [
  {
    sentAt: '2026-10-26T08:00:00Z',
    title: 'Funds available',
    body: 'Good news! £2,160.00 from your recent sales is now available in your seller balance. Withdrawal PO-0001-01 to your linked bank account has been requested.',
    recordId: 'rec-0001-020',
  },
];

/** Maya's own purchases. Private to her: Alibi never shares this. */
export const purchases: Purchase[] = [
  {
    boughtAt: '2026-10-14T10:00:00Z',
    title: 'Prescription eyewear',
    amountMinor: 4500,
    recordId: 'rec-0001-019',
  },
];

export const totals = {
  salesMinor: 240000,
  refundsMinor: 18000,
  feesMinor: 6000,
  netMinor: 216000,
} as const;

const dateFmt = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});
const timeFmt = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
});

/** "5 Oct 2026" from a UTC ISO time. */
export function day(iso: string): string {
  return dateFmt.format(new Date(iso));
}

/** "5 Oct 2026, 10:00 UTC" from a UTC ISO time. */
export function when(iso: string): string {
  return `${dateFmt.format(new Date(iso))}, ${timeFmt.format(new Date(iso))} UTC`;
}
