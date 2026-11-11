/**
 * The simulation's step guide: where the visitor is in Maya's case and where to go next. The
 * marketplace (/shop/), the bank (/bank/) and Alibi (/simulate/) are parts of one site. Pure, so it
 * is unit tested; SimGuide.astro renders it.
 */
import type { DemoStage } from '../demo';

export interface GuideStep {
  number: number;
  of: number;
  text: string;
  /** Where to do it. */
  path: string;
  cta: string;
  /** Where to go next once on `path`, if anywhere. */
  then?: { path: string; cta: string };
}

const OF = 4;

export function guideStep(
  stage: DemoStage | null | undefined,
  requestId: string,
  packageId: string | null,
): GuideStep {
  switch (stage) {
    case 'withdrawn':
    case 'flagged':
      return {
        number: 2,
        of: OF,
        text: 'Bank A flags the £2,160 credit and asks for Maya’s alibi',
        path: '/bank/cases/FC-0147/',
        cta: 'Open the case at Bank A',
      };
    case 'requested':
      return {
        number: 3,
        of: OF,
        text: 'Maya runs the investigation, reviews what would be shared, approves and sends',
        // Her dashboard first (the request is waiting there); from any other page, including the
        // request itself, this is the way back to it.
        path: '/simulate/',
        cta: 'Go to Maya’s Alibi dashboard',
        then: {
          path: `/simulate/requests/${encodeURIComponent(requestId)}/`,
          cta: 'Open Bank A’s request',
        },
      };
    case 'received':
      return {
        number: 4,
        of: OF,
        text: 'Bank A’s analyst checks the package and records a decision. Reset to try another path',
        path: packageId ? `/bank/packages/${encodeURIComponent(packageId)}/` : '/bank/inbox/',
        cta: 'Open the package',
      };
    default:
      return {
        number: 1,
        of: OF,
        text: 'Maya withdraws her £2,160 seller payout to her bank',
        path: '/shop/seller/payouts/',
        cta: 'Go to the marketplace',
      };
  }
}
