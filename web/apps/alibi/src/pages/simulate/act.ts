/**
 * POST /simulate/act: Maya's actions from the simulation dashboard, for the live demo. Off unless
 * the site sets ALIBI_SIMULATE_ACTIONS=1 (pnpm demo does); then it is the same handler as
 * /app/act (src/lib/holderAct.ts), landing back on /simulate/ screens.
 */
import type { APIRoute } from 'astro';
import { ALIBI_SIMULATE_ACTIONS } from 'astro:env/server';

import { holderAct } from '@/lib/holderAct';

export const prerender = false;

export const POST: APIRoute = (context) =>
  ALIBI_SIMULATE_ACTIONS === '1'
    ? holderAct(context, '/simulate')
    : new Response('The simulation is read-only here.', { status: 403 });
