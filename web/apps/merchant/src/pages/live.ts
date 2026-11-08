/**
 * GET /live/: the live demo's version for this site's pages to poll (@alibi/ui/live). Only the
 * version and stage leave the server; the director's credential never does.
 */
import type { APIRoute } from 'astro';
import { ALIBI_DEMO_ORIGIN, ALIBI_DEMO_TOKEN } from 'astro:env/server';

import { demoState, liveBody } from '@alibi/ui/demo';

export const prerender = false;

export const GET: APIRoute = async () =>
  new Response(
    JSON.stringify(
      liveBody(await demoState({ origin: ALIBI_DEMO_ORIGIN, token: ALIBI_DEMO_TOKEN })),
    ),
    { headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } },
  );
