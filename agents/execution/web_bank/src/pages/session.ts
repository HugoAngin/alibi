/**
 * POST /session/: analyst sign-in with the demo passcode (Worker secret ANALYST_PASSCODE), issuing
 * a signed session cookie (BANK_SESSION_SECRET). POST /session/?out=1 signs out. Same-origin only.
 * If either secret is unset, sign-in is disabled and live actions stay impossible.
 */
import type { APIRoute } from 'astro';
import { ANALYST_PASSCODE, BANK_SESSION_SECRET } from 'astro:env/server';

import { analyst as demoAnalyst } from '@/data/cases';
import { SESSION_COOKIE, SESSION_TTL_MS, makeSession, safeEqual } from '@/lib/session';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect, url }) => {
  if (request.headers.get('origin') !== url.origin)
    return new Response('Cross-site request refused.', { status: 403 });
  if (url.searchParams.get('out') === '1') {
    cookies.delete(SESSION_COOKIE, { path: '/' });
    return redirect('/sign-in/', 303);
  }
  const form = await request.formData();
  const passcode = String(form.get('passcode') ?? '');
  if (!ANALYST_PASSCODE || !BANK_SESSION_SECRET || !safeEqual(passcode, ANALYST_PASSCODE))
    return redirect('/sign-in/?failed=1', 303);
  const token = await makeSession(BANK_SESSION_SECRET, demoAnalyst.name, Date.now());
  cookies.set(SESSION_COOKIE, token, {
    path: '/',
    httpOnly: true,
    secure: url.protocol === 'https:',
    sameSite: 'strict',
    maxAge: Math.floor(SESSION_TTL_MS / 1000),
  });
  return redirect('/inbox/', 303);
};
