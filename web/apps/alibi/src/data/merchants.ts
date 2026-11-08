/**
 * Merchants Alibi knows how to connect to, keyed by the `merchant` parameter
 * of a connect link. An unknown ID is shown as unverified, never trusted
 * (SPEC: label unknown merchants as unverified).
 */

export interface MerchantEntry {
  name: string;
  /** What the merchant will send to the passport once connected. */
  shares: string[];
}

export const merchants: Record<string, MerchantEntry> = {
  'mock-marketplace': {
    name: 'Mock Marketplace',
    shares: [
      'Sales and their status',
      'Refunds',
      'Platform fees',
      'Payouts and settlement records',
    ],
  },
};
