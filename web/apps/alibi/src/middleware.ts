/**
 * The whole demo is this one site: Alibi, the bank console under /bank/ and the marketplace under
 * /shop/. Three ways it runs, chosen by the environment:
 *
 * - Simulated (ALIBI_SIMULATION=1: the deployed site and `pnpm dev`). Every backend call is
 *   answered from the recorded demo (@alibi/ui/sim), per visitor, with no backend, key or account.
 *   The visitor acts as Maya: `/simulate/` is her dashboard, `/app/` pages with a simulation twin
 *   redirect to it, and the other `/app/` pages (merchants, banks, exports) show her data in place.
 * - The local live demo (`pnpm demo`, src/lib/demoHolder.ts): the real backend on this machine.
 *   Requests for any non-loopback host are refused, and every holder route needs the signed demo
 *   session (sign in at /demo-sign-in/ as Maya).
 * - Neither: there is no sign-in, so `/app/` is closed and sends visitors to `/simulate/`.
 *
 * Either way, links inside `/simulate/` pages are kept inside the simulation (src/lib/simulation.ts).
 */
import { defineMiddleware } from 'astro:middleware';
import type { APIContext, MiddlewareNext } from 'astro';
import { ALIBI_DEMO_HOLDER, ALIBI_SIMULATION, HOLDER_SESSION_SECRET } from 'astro:env/server';

import { runSimulated } from '@alibi/ui/sim/context';
import {
  HOLDER_COOKIE,
  demoHolderEnabled,
  isHolderRoute,
  isLoopback,
  verifyHolderSession,
} from '@/lib/demoHolder';
import { isSimulationRoute, simulationLinks, simulationTwin } from '@/lib/simulation';

function isAppRoute(pathname: string): boolean {
  return pathname === '/app' || pathname.startsWith('/app/');
}

const isRead = (context: APIContext) =>
  context.request.method === 'GET' || context.request.method === 'HEAD';

/** `/app/` without the live demo: the simulation twin when there is one, else Maya's dashboard. */
function toSimulation(context: APIContext): Response {
  const twin = simulationTwin(context.url.pathname) ?? '/simulate/';
  return context.redirect(twin + context.url.search, 303);
}

async function simulated(context: APIContext, next: MiddlewareNext): Promise<Response> {
  return runSimulated(context, () => {
    if (isAppRoute(context.url.pathname) && isRead(context)) {
      const twin = simulationTwin(context.url.pathname);
      if (twin) return Promise.resolve(context.redirect(twin + context.url.search, 303));
    }
    return next();
  });
}

async function demoHolder(context: APIContext, next: MiddlewareNext): Promise<Response> {
  if (!isLoopback(context.url.hostname)) {
    return new Response('Demo holder mode serves this machine only (127.0.0.1).', { status: 403 });
  }

  const holder = await verifyHolderSession(
    HOLDER_SESSION_SECRET,
    context.cookies.get(HOLDER_COOKIE)?.value,
    Date.now(),
  );
  context.locals.demoHolder = holder;

  if (isHolderRoute(context.url.pathname) && !holder) {
    if (isRead(context)) {
      const back = context.url.pathname + context.url.search;
      return context.redirect(`/demo-sign-in/?next=${encodeURIComponent(back)}`, 303);
    }
    return new Response('Sign in first.', { status: 401 });
  }
  return next();
}

function closed(context: APIContext, next: MiddlewareNext): Promise<Response> | Response {
  if (!isAppRoute(context.url.pathname)) return next();
  return isRead(context) ? toSimulation(context) : new Response('Not available.', { status: 404 });
}

export const onRequest = defineMiddleware(async (context, next) => {
  const response =
    ALIBI_SIMULATION === '1'
      ? await simulated(context, next)
      : demoHolderEnabled(ALIBI_DEMO_HOLDER, HOLDER_SESSION_SECRET)
        ? await demoHolder(context, next)
        : await closed(context, next);

  if (
    !isSimulationRoute(context.url.pathname) ||
    !(response.headers.get('content-type') ?? '').includes('text/html')
  )
    return response;
  const html = simulationLinks(await response.text());
  const headers = new Headers(response.headers);
  headers.delete('content-length');
  return new Response(html, { status: response.status, headers });
});
