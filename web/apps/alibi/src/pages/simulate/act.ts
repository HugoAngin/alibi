/**
 * POST /simulate/act: Maya's actions from the simulation dashboard, for the live demo. Off unless
 * the site is simulated or sets ALIBI_SIMULATE_ACTIONS=1 (pnpm demo does); then it is the same handler as
 * /app/act (src/lib/holderAct.ts), landing back on /simulate/ screens.
 */
import type { APIRoute } from 'astro';
import { holderAct } from '@/lib/holderAct';
import { simulationActs } from '@/lib/simulationActs';

export const prerender = false;

export const POST: APIRoute = (context) =>
  simulationActs()
    ? holderAct(context, '/simulate')
    : new Response('The simulation is read-only here.', { status: 403 });
