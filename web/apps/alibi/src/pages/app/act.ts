/**
 * POST /app/act: forwards one of Maya's actions (run investigation, remove an
 * item, approve) to the backend action URL the screen's context supplied,
 * then sends her back to the screen with the outcome. The backend decides
 * everything; a refusal is shown in its own words (shared/CONTRACTS.md).
 */
import type { APIRoute } from 'astro';
import { ALIBI_API_ORIGIN } from 'astro:env/server';

import { forwardAction } from '@/lib/api/client';
import { backendHeaders, safeReturn, setFlash } from '@/lib/screens';

export const prerender = false;

export const POST: APIRoute = async ({ request, cookies, redirect }) => {
  const form = await request.formData();
  const path = String(form.get('action') ?? '');
  const returnTo = safeReturn(form.get('return_to') as string | null);

  const body: Record<string, string> = {};
  for (const [key, value] of form) {
    if (key !== 'action' && key !== 'return_to' && typeof value === 'string') body[key] = value;
  }

  const result = await forwardAction({
    origin: ALIBI_API_ORIGIN,
    path,
    body,
    headers: backendHeaders(),
  });
  if (result.kind === 'refused') setFlash(cookies, { kind: 'refused', refusal: result.refusal });
  else if (result.kind === 'unavailable')
    setFlash(cookies, { kind: 'unavailable', message: result.message });
  else
    setFlash(cookies, {
      kind: 'ok',
      message: 'Done. The screen shows the backend’s current state.',
    });

  return redirect(returnTo, 303);
};
