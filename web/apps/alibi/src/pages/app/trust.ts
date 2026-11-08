/**
 * POST /app/trust: trust or stop trusting one bank (src/lib/trust.ts). Same-origin only, and behind
 * sign-in like every /app route (src/middleware.ts). Changes nothing on the backend: it only marks
 * the bank's requests and puts one-tap approval on them.
 */
import type { APIRoute } from 'astro';

import { TRUST_COOKIE, isAudience, parseTrusted, withTrust } from '@/lib/trust';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect, url }) => {
  if (request.headers.get('origin') !== url.origin) {
    return new Response('Cross-site request refused.', { status: 403 });
  }
  const form = await request.formData();
  const audience = String(form.get('audience') ?? '');
  if (!isAudience(audience)) return new Response('Unknown bank.', { status: 400 });

  const value = withTrust(
    parseTrusted(cookies.get(TRUST_COOKIE)?.value),
    audience,
    form.get('trusted') === '1',
  );
  cookies.set(TRUST_COOKIE, value, {
    path: '/',
    httpOnly: true,
    sameSite: 'lax',
    secure: url.protocol === 'https:',
    maxAge: 60 * 60 * 24 * 365,
  });
  return redirect(`/app/banks/${encodeURIComponent(audience)}/`, 303);
};
