/**
 * What the landing page says. Copy lives here, not in components, so it can
 * be rewritten without touching layout. Draft wording; the design pass and the
 * deck will refine it (agents/research/out/DECK.md).
 */

export const product = {
  name: 'Alibi',
  tagline: 'Your money’s alibi, ready before anyone asks.',
  lede: 'Alibi is a financial passport you control. It links your bank payments to the sales, refunds and payouts that explain them, so when your bank asks about a payment you can answer in minutes and share only what the question needs.',
} as const;

export const features = [
  {
    title: 'Evidence before the question',
    body: 'Connect your marketplace and your bank once. Alibi matches payouts to credits in the background and warns you about anything it cannot yet explain.',
  },
  {
    title: 'Challenged, not just matched',
    body: 'Matching amounts are not proof. Alibi checks that a payout actually reached your bank, and when, before it calls an explanation supported.',
  },
  {
    title: 'You approve every disclosure',
    body: 'Your bank sees the smallest package that answers its question. You see exactly what is included, what is left out and why, and you approve it for one named bank.',
  },
  {
    title: 'One passport, many requests',
    body: 'A second bank with a different question gets its own tailored answer from the same history, with no re-uploading and a fresh approval.',
  },
] as const;

/** How Alibi differs from what people do today. */
export const differentiation = [
  {
    today: 'Screenshots and statements sent by email',
    alibi: 'Signed records the bank can verify itself',
  },
  {
    today: 'Share everything and hope it is enough',
    alibi: 'Share only what the question needs',
  },
  {
    today: 'An amount that happens to match',
    alibi: 'A payout traced to the credit, with its timing checked',
  },
  {
    today: 'Start from scratch for every request',
    alibi: 'Reuse one history, with a new approval each time',
  },
] as const;

export const audiences = [
  {
    title: 'For people',
    body: 'Resellers, freelancers and anyone paid through a platform, whose bank asks them to explain money coming in.',
  },
  {
    title: 'For merchants',
    body: 'Add “Connect to my Alibi” to account settings or checkout so customers can carry your records to their bank.',
  },
  {
    title: 'For banks',
    body: 'Add “Request customer’s Alibi” to your case tools and receive verifiable, minimal answers instead of document dumps.',
  },
] as const;
