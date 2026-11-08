/**
 * POST /act/: the console's two analyst actions (decision, follow-up). The backend path is built
 * on the server from a fixed kind and a validated ID (src/lib/actions.ts); the form never supplies
 * a path. Requires a same-origin POST and a signed analyst session; otherwise nothing is sent
 * upstream and the Bank A credential is never used. Refusals from the backend are shown verbatim.
 */
import type { APIRoute } from 'astro';
import { ALIBI_API_ORIGIN, BANK_A_TOKEN, BANK_SESSION_SECRET } from 'astro:env/server';

import { forwardAction } from '@alibi/ui/api';

import { handleAct } from '@/lib/actions';
import { backendHeaders, safeReturn, setFlash } from '@/lib/screens';
import { SESSION_COOKIE, verifySession } from '@/lib/session';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect, url }) => {
  const form = await request.formData();
  const fields: Record<string, string> = {};
  for (const [key, value] of form) if (typeof value === 'string') fields[key] = value;
  const returnTo = safeReturn(fields.return_to ?? null);

  const analyst = await verifySession(BANK_SESSION_SECRET, cookies.get(SESSION_COOKIE)?.value, Date.now());
  const outcome = await handleAct(
    { fields, requestOrigin: request.headers.get('origin'), siteOrigin: url.origin, analyst },
    { origin: ALIBI_API_ORIGIN, headers: backendHeaders(BANK_A_TOKEN), forward: forwardAction },
  );

  if (outcome.kind === 'forbidden') {
    if (outcome.status === 403) return new Response(outcome.message, { status: 403 });
    setFlash(cookies, { kind: 'unavailable', message: outcome.message });
    return redirect(outcome.status === 401 ? '/sign-in/' : returnTo, 303);
  }
  const result = outcome.result;
  if (result.kind === 'refused') setFlash(cookies, { kind: 'refused', refusal: result.refusal });
  else if (result.kind === 'unavailable') setFlash(cookies, { kind: 'unavailable', message: result.message });
  else setFlash(cookies, { kind: 'ok', message: 'Recorded. The screen shows the backend’s current state.' });
  return redirect(returnTo, 303);
};
