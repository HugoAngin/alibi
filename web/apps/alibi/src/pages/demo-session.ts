/**
 * POST /demo-session/: signs in as Maya in demo holder mode (src/lib/demoHolder.ts), issuing the
 * signed holder cookie. POST /demo-session/?out=1 signs out. Same-origin only; 404 unless demo
 * holder mode is on (the middleware has already refused non-loopback hosts).
 */
import type { APIRoute } from 'astro';
import { ALIBI_DEMO_HOLDER, HOLDER_SESSION_SECRET } from 'astro:env/server';

import {
  DEMO_HOLDER,
  HOLDER_COOKIE,
  HOLDER_TTL_MS,
  demoHolderEnabled,
  makeHolderSession,
  safeNext,
} from '@/lib/demoHolder';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect, url }) => {
  if (!demoHolderEnabled(ALIBI_DEMO_HOLDER, HOLDER_SESSION_SECRET) || !HOLDER_SESSION_SECRET) {
    return new Response('Not found.', { status: 404 });
  }
  if (request.headers.get('origin') !== url.origin) {
    return new Response('Cross-site request refused.', { status: 403 });
  }
  if (url.searchParams.get('out') === '1') {
    cookies.delete(HOLDER_COOKIE, { path: '/' });
    return redirect('/', 303);
  }
  const form = await request.formData();
  const token = await makeHolderSession(HOLDER_SESSION_SECRET, DEMO_HOLDER, Date.now());
  cookies.set(HOLDER_COOKIE, token, {
    path: '/',
    httpOnly: true,
    secure: url.protocol === 'https:',
    sameSite: 'strict',
    maxAge: Math.floor(HOLDER_TTL_MS / 1000),
  });
  return redirect(safeNext(String(form.get('next') ?? '')), 303);
};
