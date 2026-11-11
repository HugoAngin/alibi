/**
 * POST /demo-reset/: the presenter's Reset demo button. Every service goes back to the start of
 * Maya's case (alibi/demo_director.py): payout not withdrawn, nothing flagged, nothing sent. Needs
 * a same-origin POST and a signed analyst session; the other sites follow within two seconds
 * through their live polling.
 */
import type { APIRoute } from 'astro';
import { ALIBI_DEMO_ORIGIN, ALIBI_DEMO_TOKEN } from 'astro:env/server';

import { demoStep } from '@alibi/ui/demo';

import { currentAnalyst } from '@/bank/lib/analyst';
import { setFlash } from '@/bank/lib/screens';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect, url }) => {
  if (request.headers.get('origin') !== url.origin)
    return new Response('Cross-site request refused.', { status: 403 });
  const analyst = await currentAnalyst(cookies);
  if (!analyst) return redirect('/bank/sign-in/', 303);
  const result = await demoStep({ origin: ALIBI_DEMO_ORIGIN, token: ALIBI_DEMO_TOKEN }, 'reset');
  if (result.kind === 'refused') setFlash(cookies, { kind: 'refused', refusal: result.refusal });
  else if (result.kind === 'unavailable')
    setFlash(cookies, { kind: 'unavailable', message: result.message });
  else
    setFlash(cookies, {
      kind: 'ok',
      message: 'Demo reset. Maya’s payout is back in her seller balance and nothing is flagged.',
    });
  return redirect('/bank/', 303);
};
