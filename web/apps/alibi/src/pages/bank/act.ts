/**
 * POST /act/: the console's three analyst actions (request, decision, follow-up). The backend path is built
 * on the server from a fixed kind and a validated ID (src/lib/actions.ts); the form never supplies
 * a path. Requires a same-origin POST and a signed analyst session; otherwise nothing is sent
 * upstream and the Bank A credential is never used. Refusals from the backend are shown verbatim.
 */
import type { APIRoute } from 'astro';
import {
  ALIBI_API_ORIGIN,
  ALIBI_DEMO_ORIGIN,
  ALIBI_DEMO_TOKEN,
  BANK_API_ORIGIN,
  BANK_A_TOKEN,
} from 'astro:env/server';

import { forwardAction } from '@alibi/ui/api';
import { demoEnabled, demoStep } from '@alibi/ui/demo';

import { handleAct, type Forward } from '@/bank/lib/actions';
import { currentAnalyst } from '@/bank/lib/analyst';
import { bankTarget } from '@/bank/lib/bankApi';
import { backendHeaders, safeReturn, setFlash } from '@/bank/lib/screens';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect, url }) => {
  const form = await request.formData();
  const fields: Record<string, string> = {};
  for (const [key, value] of form) if (typeof value === 'string') fields[key] = value;
  const returnTo = safeReturn(fields.return_to ?? null);

  const analyst = await currentAnalyst(cookies);
  // forwardAction only JSON-serialises the body; its type says string values, but the bank needs
  // `requested_evidence` as a list, so the planned body passes through as is.
  // In the live demo the request goes through the director, which first moves the shared clock
  // to the request's issue time, then has the bank issue and forward it (alibi/demo_director.py).
  const demo = { origin: ALIBI_DEMO_ORIGIN, token: ALIBI_DEMO_TOKEN };
  const forward: Forward = (o) =>
    fields.kind === 'request' && demoEnabled(demo)
      ? demoStep(demo, 'request', { recipient: 'bank_a' })
      : forwardAction({ ...o, body: o.body as unknown as Record<string, string> });
  const outcome = await handleAct(
    { fields, requestOrigin: request.headers.get('origin'), siteOrigin: url.origin, analyst },
    {
      target: bankTarget(BANK_API_ORIGIN, ALIBI_API_ORIGIN),
      headers: backendHeaders(BANK_A_TOKEN),
      forward,
    },
  );

  if (outcome.kind === 'forbidden') {
    if (outcome.status === 403) return new Response(outcome.message, { status: 403 });
    setFlash(cookies, { kind: 'unavailable', message: outcome.message });
    return redirect(outcome.status === 401 ? '/bank/sign-in/' : returnTo, 303);
  }
  const result = outcome.result;
  if (result.kind === 'refused') setFlash(cookies, { kind: 'refused', refusal: result.refusal });
  else if (result.kind === 'unavailable')
    setFlash(cookies, { kind: 'unavailable', message: result.message });
  else
    setFlash(cookies, {
      kind: 'ok',
      message:
        fields.kind === 'request'
          ? 'Request sent. The customer has it in their Alibi.'
          : 'Recorded. The screen shows the backend’s current state.',
    });
  return redirect(returnTo, 303);
};
