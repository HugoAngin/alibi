/**
 * Maya's demo passport, as the app view shows it before any backend is wired.
 *
 * SYNTHETIC: a fictional person, merchant and bank. The identifiers and
 * amounts mirror the team's canonical fixture, development case 1 in
 * `agents/data/out/cases/case_0001.json`; tests/demo.test.ts fails if they
 * drift apart. Amounts are integer pence, times are UTC.
 */

export const holder = { name: 'Maya', subjectRef: 'acct_maya_001' } as const;

/** The credit the bank asks about: £2,400 sales − £180 refunds − £60 fees. */
export const credit = {
  txnId: 'txn-0001-004',
  bookedAt: '2026-10-28T06:30:00Z',
  description: 'MOCK MARKETPLACE PAYOUT PO-0001-01',
  amountMinor: 216000,
  currency: 'GBP',
  payoutReference: 'PO-0001-01',
} as const;

export const breakdown = {
  salesMinor: 240000,
  refundsMinor: 18000,
  feesMinor: 6000,
} as const;

export const connections = [
  { id: 'resalehub', name: 'ResaleHub', kind: 'Marketplace', trust: 'Issuer verified' },
  { id: 'bank-feed', name: 'Current account', kind: 'Bank feed', trust: 'Bank observed' },
] as const;

/** Raised before any bank request exists (SPEC: proactive monitoring). */
export const alert = {
  title: 'Your £2,160 credit has no settlement proof yet',
  body: 'ResaleHub says the money was released to your seller balance. That is not proof it reached your bank. Alibi will look for the payout record.',
  createdAt: '2026-10-28T07:00:00Z',
} as const;

export const request = {
  requestId: 'rfc-0001-01',
  from: 'Bank A',
  questions: ['Where did this money come from?', 'How is the amount made up?'],
  issuedAt: '2026-11-02T09:00:00Z',
} as const;

/** Sales less refunds less fees. Computed, never typed in. */
export function netMinor(b: typeof breakdown = breakdown): number {
  return b.salesMinor - b.refundsMinor - b.feesMinor;
}

const gbp = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' });

/** `216000` → `£2,160.00`. */
export function formatGBP(minor: number): string {
  return gbp.format(minor / 100);
}
