/**
 * The simulated marketplace. SYNTHETIC: a fictional shop and customer. The
 * merchant ID is the one Alibi's connection registry knows
 * (apps/alibi/src/data/merchants.ts).
 */

export const shop = {
  merchantId: 'mock-marketplace',
  name: 'Mock Marketplace',
  tagline: 'Second-hand clothes, sold by people like you.',
} as const;

export const customer = { name: 'Maya', email: 'maya@example.test' } as const;

export const listings = [
  { id: 'lot-1', title: 'Denim jacket, size 10', priceMinor: 3000 },
  { id: 'lot-2', title: 'Wool coat, size 12', priceMinor: 4500 },
  { id: 'lot-3', title: 'Trainers, UK 6', priceMinor: 2500 },
] as const;

const gbp = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' });

export function formatGBP(minor: number): string {
  return gbp.format(minor / 100);
}
