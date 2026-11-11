/**
 * POST /simulate/reset/: the Reset demo button on Maya's simulation bar. Only when Maya's
 * dashboard can act (src/lib/simulation.ts) and from a same-origin POST. On a simulated site the
 * visitor's run goes back to the start of the recording; in the local live demo the director puts
 * every service back to the start of the case (alibi/demo_director.py). The other sites follow by
 * live polling.
 */
import type { APIRoute } from 'astro';
import { ALIBI_DEMO_ORIGIN, ALIBI_DEMO_TOKEN } from 'astro:env/server';

import { demoStep } from '@alibi/ui/demo';
import { simulationActs } from '@/lib/simulationActs';

export const prerender = false;

export const POST: APIRoute = async ({ request, redirect, url }) => {
  if (!simulationActs()) return new Response('The simulation is read-only here.', { status: 403 });
  if (request.headers.get('origin') !== url.origin)
    return new Response('Cross-site request refused.', { status: 403 });
  await demoStep({ origin: ALIBI_DEMO_ORIGIN, token: ALIBI_DEMO_TOKEN }, 'reset');
  return redirect('/simulate/', 303);
};
