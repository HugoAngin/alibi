/**
 * Deployed: Clerk on every request, and the holder app (`/app/*`) needs a signed-in Clerk user, so
 * nobody else can see or approve for Maya (eval gate b): a signed-out GET goes to /sign-in/,
 * anything else gets 401. Local demo (`pnpm demo`, src/lib/demoHolder.ts): Clerk is not called at
 * all, so no Clerk key is needed; requests for any non-loopback host are refused, and every holder
 * route needs the signed demo session (sign in at /demo-sign-in/ as Maya).
 *
 * Either way, `/simulate/` stays open: Maya's dashboard for any visitor, read-only (every action
 * disabled). Its links into the app are kept inside the simulation (src/lib/simulation.ts).
 */
import { clerkMiddleware } from '@clerk/astro/server';
import type { AuthFn } from '@clerk/astro/server';
import { defineMiddleware } from 'astro:middleware';
import type { APIContext, MiddlewareNext } from 'astro';
import { ALIBI_DEMO_HOLDER, HOLDER_SESSION_SECRET } from 'astro:env/server';

import {
  DEMO_USER_ID,
  HOLDER_COOKIE,
  demoHolderEnabled,
  isHolderRoute,
  isLoopback,
  verifyHolderSession,
} from '@/lib/demoHolder';
import { isSimulationRoute, simulationLinks } from '@/lib/simulation';

/** The holder app's pages and endpoints (Clerk 4.1 has no createRouteMatcher). */
function isAppRoute(pathname: string): boolean {
  return pathname === '/app' || pathname.startsWith('/app/');
}

const clerk = clerkMiddleware((auth, context, next) => {
  if (isAppRoute(context.url.pathname) && !auth().userId) {
    if (context.request.method === 'GET' || context.request.method === 'HEAD')
      return context.redirect('/sign-in/', 303);
    return new Response('Sign in first.', { status: 401 });
  }
  return next();
});

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
  // Enough of Clerk's auth() for <Show when="signed-in|signed-out">; nothing else reads it.
  context.locals.auth = (() => ({
    userId: holder ? DEMO_USER_ID : null,
    has: () => false,
  })) as unknown as AuthFn;

  if (isHolderRoute(context.url.pathname) && !holder) {
    if (context.request.method === 'GET' || context.request.method === 'HEAD') {
      const back = context.url.pathname + context.url.search;
      return context.redirect(`/demo-sign-in/?next=${encodeURIComponent(back)}`, 303);
    }
    return new Response('Sign in first.', { status: 401 });
  }
  return next();
}

export const onRequest = defineMiddleware(async (context, next) => {
  const response = demoHolderEnabled(ALIBI_DEMO_HOLDER, HOLDER_SESSION_SECRET)
    ? await demoHolder(context, next)
    : // Clerk's handler is typed loosely (void | Response) but always returns the response.
      ((await clerk(context, next)) as Response);

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
