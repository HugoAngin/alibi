/**
 * POST /simulate/reset/: the Reset demo button on Maya's simulation bar. Only with the live demo
 * on (ALIBI_SIMULATE_ACTIONS=1) and a same-origin POST; the director puts every service back to
 * the start of the case (alibi/demo_director.py) and the other sites follow by live polling.
 */
import type { APIRoute } from 'astro';
import { ALIBI_DEMO_ORIGIN, ALIBI_DEMO_TOKEN, ALIBI_SIMULATE_ACTIONS } from 'astro:env/server';

import { demoStep } from '@alibi/ui/demo';

export const prerender = false;

export const POST: APIRoute = async ({ request, redirect, url }) => {
  if (ALIBI_SIMULATE_ACTIONS !== '1')
    return new Response('The simulation is read-only here.', { status: 403 });
  if (request.headers.get('origin') !== url.origin)
    return new Response('Cross-site request refused.', { status: 403 });
  await demoStep({ origin: ALIBI_DEMO_ORIGIN, token: ALIBI_DEMO_TOKEN }, 'reset');
  return redirect('/simulate/', 303);
};
