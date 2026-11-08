/**
 * What the product site says. Copy lives here, not in components, so it can
 * be rewritten without touching layout. Facts about Maya come from case 1.
 */

export const product = {
  name: 'Alibi',
  tagline: 'Your money’s alibi, ready before anyone asks',
  lede: 'Alibi keeps the evidence behind your payments ready: it links what lands in your bank to the sales, refunds and payouts that explain it, so when a bank asks, you answer in minutes and share only what the question needs.',
} as const;

export interface CoreFeature {
  id: string;
  index: string;
  eyebrow: string;
  title: string;
  body: string;
  points: string[];
}

/** The three things that set Alibi apart, in order of importance. */
export const core: CoreFeature[] = [
  {
    id: 'engine',
    index: '01',
    eyebrow: 'The Alibi engine',
    title: 'Four agents investigate, code decides',
    body: 'When a bank asks about a payment, four Gemini agents work on your passport. They read messy merchant messages, look for counter-evidence and fetch the one record that settles the question. Then code checks every reference, date and penny before anything is called true.',
    points: [
      'Matcher links payouts to bank credits when the references are messy',
      'Detective turns the bank’s question into claims and spots what is missing',
      'Challenger hunts for counter-evidence and fetches the deciding record',
      'Curator proposes the smallest set of evidence that answers the question',
    ],
  },
  {
    id: 'integrations',
    index: '02',
    eyebrow: 'Merchant and bank integration',
    title: 'Two buttons connect the whole loop',
    body: 'Merchants add “Connect to my alibi” to account settings or checkout, and their signed records flow into your passport in the background. Banks add “Request customer’s alibi” to their fraud tools and get a precise, verifiable answer instead of a pile of screenshots.',
    points: [
      'Signed records from merchants, read-only data from your bank',
      'A bank asks a specific question about a specific payment',
      'The answer arrives signed, so the bank can verify it itself',
    ],
  },
  {
    id: 'control',
    index: '03',
    eyebrow: 'You stay in control',
    title: 'Your financial context, on your terms',
    body: 'Everything that explains your money lives in one passport you own. You see exactly what each bank would receive and what stays private, and nothing leaves until you approve it, one named bank at a time.',
    points: [
      'Approve the exact package for one named bank',
      'Rent, groceries and anything you mark private stay out',
      'Answer the next bank from the same history, with a fresh approval',
    ],
  },
];
