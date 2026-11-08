/**
 * Deployed: Clerk on every request. Local demo (`pnpm demo`, src/lib/demoHolder.ts): Clerk is not
 * called at all, so no Clerk key is needed; requests for any non-loopback host are refused, and
 * every holder route needs the signed demo session (sign in at /demo-sign-in/ as Maya).
 */
import { clerkMiddleware } from '@clerk/astro/server';
import type { AuthFn } from '@clerk/astro/server';
import { defineMiddleware } from 'astro:middleware';
import { ALIBI_DEMO_HOLDER, HOLDER_SESSION_SECRET } from 'astro:env/server';

import {
  DEMO_USER_ID,
  HOLDER_COOKIE,
  demoHolderEnabled,
  isHolderRoute,
  isLoopback,
  verifyHolderSession,
} from '@/lib/demoHolder';

const clerk = clerkMiddleware();

export const onRequest = defineMiddleware(async (context, next) => {
  if (!demoHolderEnabled(ALIBI_DEMO_HOLDER, HOLDER_SESSION_SECRET)) {
    // Clerk's handler is typed loosely (void | Response) but always returns the response.
    return (await clerk(context, next)) as Response;
  }

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
});
