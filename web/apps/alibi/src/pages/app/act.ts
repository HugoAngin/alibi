/**
 * POST /app/act: forwards one of Maya's actions (run investigation, remove an
 * item, approve) to the backend action URL the screen's context supplied,
 * then sends her back to the screen with the outcome. The backend decides
 * everything; a refusal is shown in its own words (shared/CONTRACTS.md).
 *
 * Approval stays on the review page. Dispatch, and a trusted bank's one-tap approve-and-send
 * (`and_send=1`), go to the receipt or refusal page.
 */
import type { APIRoute } from 'astro';
import { ALIBI_API_ORIGIN } from 'astro:env/server';

import { forwardAction } from '@alibi/ui/api';
import { asDispatch } from '@/lib/api/guards';
import { backendHeaders, setFlash } from '@/lib/screens';

export const prerender = false;

const REQUEST_ACTION =
  /^\/requests\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\/(run|restore|approve|dispatch|remove\/(?:record|txn)\/[A-Za-z0-9][A-Za-z0-9._-]{0,127})$/;
const APPROVAL_FIELDS = ['digest', 'audience', 'policy_version'] as const;

export const POST: APIRoute = async ({ request, cookies, redirect }) => {
  if (request.headers.get('origin') !== new URL(request.url).origin) {
    return new Response('Cross-site request refused.', { status: 403 });
  }

  const form = await request.formData();
  const path = String(form.get('action') ?? '');
  const action = REQUEST_ACTION.exec(path);
  if (!action) return new Response('Unknown request action.', { status: 400 });

  const [, id, kind] = action;
  const returnTo = `/app/requests/${id}/`;
  const body: Record<string, string> = {};
  if (kind === 'approve') {
    for (const field of APPROVAL_FIELDS) body[field] = String(form.get(field) ?? '');
  }

  let result = await forwardAction({
    origin: ALIBI_API_ORIGIN,
    path,
    body,
    encoding: 'form',
    headers: backendHeaders(),
  });

  // A trusted bank's one-tap button (src/lib/trust.ts): the holder's single tap approves, and once
  // the backend accepts that approval, the same request is sent. A refused approval sends nothing.
  const andSend = kind === 'approve' && form.get('and_send') === '1' && result.kind === 'ok';
  if (andSend) {
    result = await forwardAction({
      origin: ALIBI_API_ORIGIN,
      path: `/requests/${id}/dispatch`,
      body: {},
      encoding: 'form',
      headers: backendHeaders(),
    });
  }

  if (result.kind === 'refused') {
    setFlash(cookies, { kind: 'refused', refusal: result.refusal });
  } else if (result.kind === 'unavailable') {
    setFlash(cookies, { kind: 'unavailable', message: result.message });
  } else {
    const dispatch = asDispatch(result.data);
    setFlash(
      cookies,
      dispatch
        ? { kind: 'dispatch', dispatch }
        : { kind: 'ok', message: 'Done. The screen shows the backend’s current state.' },
    );
  }

  return redirect(kind === 'dispatch' || andSend ? `${returnTo}sent/` : returnTo, 303);
};
