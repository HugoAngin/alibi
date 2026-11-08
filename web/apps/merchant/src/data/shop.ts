/**
 * The simulated marketplace. SYNTHETIC. Names and sales follow the team's
 * canonical fixture: Maya (`maya-001`) is a seller on Mock Marketplace
 * (`mock-marketplace`), and her sales below are case 1's order confirmations
 * (`agents/data/out/receipts/receipts.json`, rcpt-01 to rcpt-04).
 * tests/shop.test.ts fails if they drift. The merchant ID is the one Alibi's
 * connection registry knows (apps/alibi/src/data/merchants.ts).
 */

export const shop = {
  merchantId: 'mock-marketplace',
  name: 'Mock Marketplace',
  tagline: 'Second-hand clothes, sold by people like you.',
} as const;

/** The signed-in seller on the account pages. */
export const seller = { name: 'Maya', subjectRef: 'maya-001' } as const;

export interface Sale {
  /** Order reference, as on the order confirmation. */
  reference: string;
  receiptId: string;
  title: string;
  priceMinor: number;
  sellerFeeMinor: number;
  soldOn: string;
}

export const sales: Sale[] = [
  {
    reference: 'MM-48001',
    receiptId: 'rcpt-01',
    title: 'Vintage denim jacket',
    priceMinor: 30000,
    sellerFeeMinor: 750,
    soldOn: '2026-10-05',
  },
  {
    reference: 'MM-48017',
    receiptId: 'rcpt-02',
    title: 'Wool coat, size 10',
    priceMinor: 25000,
    sellerFeeMinor: 625,
    soldOn: '2026-10-06',
  },
  {
    reference: 'MM-48042',
    receiptId: 'rcpt-03',
    title: 'Leather boots and two scarves',
    priceMinor: 40000,
    sellerFeeMinor: 1000,
    soldOn: '2026-10-07',
  },
  {
    reference: 'MM-48107',
    receiptId: 'rcpt-04',
    title: 'Bundle: 5 summer dresses',
    priceMinor: 45000,
    sellerFeeMinor: 1125,
    soldOn: '2026-10-11',
  },
];

/**
 * The checkout page shows a different person: an example buyer with an
 * example basket. Not part of case 1, and labelled as an example on screen.
 */
export const exampleBasket = [
  { title: 'Linen shirt (example item)', priceMinor: 3500 },
  { title: 'Canvas tote (example item)', priceMinor: 2000 },
] as const;

const gbp = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' });

export function formatGBP(minor: number): string {
  return gbp.format(minor / 100);
}
