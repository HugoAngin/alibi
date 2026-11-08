/**
 * POST /act/: forwards one analyst action (decision, follow-up) to the backend action path the
 * screen's context supplied, with the bank's credential, then returns to the screen with the
 * outcome. The backend decides everything; a refusal is shown in its own words
 * (shared/CONTRACTS.md, Refusals). Unreachable backend is reported as such, never as a refusal.
 */
import type { APIRoute } from 'astro';
import { ALIBI_API_ORIGIN, BANK_A_TOKEN } from 'astro:env/server';

import { forwardAction } from '@alibi/ui/api';
import { backendHeaders, safeReturn, setFlash } from '@/lib/screens';

export const prerender = false;

const CONTROL_FIELDS = new Set(['action', 'return_to']);

export const POST: APIRoute = async ({ request, cookies, redirect }) => {
  const form = await request.formData();
  const path = String(form.get('action') ?? '');
  const returnTo = safeReturn(form.get('return_to') as string | null);

  const body: Record<string, string> = {};
  for (const [key, value] of form) {
    if (!CONTROL_FIELDS.has(key) && typeof value === 'string') body[key] = value;
  }

  const result = await forwardAction({
    origin: ALIBI_API_ORIGIN,
    path,
    body,
    headers: backendHeaders(BANK_A_TOKEN),
  });

  if (result.kind === 'refused') setFlash(cookies, { kind: 'refused', refusal: result.refusal });
  else if (result.kind === 'unavailable') setFlash(cookies, { kind: 'unavailable', message: result.message });
  else setFlash(cookies, { kind: 'ok', message: 'Recorded. The screen shows the backend’s current state.' });

  return redirect(returnTo, 303);
};
