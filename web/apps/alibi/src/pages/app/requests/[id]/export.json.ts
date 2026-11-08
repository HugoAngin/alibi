/**
 * `/app/requests/<id>/export.json`: downloads the signed package the backend
 * sent for this request (`GET /requests/<id>/export.json`, UI.md), unchanged,
 * so the bytes match its digest. 404 before the package has been sent.
 */
import type { APIRoute } from 'astro';
import { ALIBI_API_ORIGIN } from 'astro:env/server';

import { holderSignedIn } from '@/components/app/home';
import { backendHeaders } from '@/lib/screens';

export const prerender = false;

const text = (body: string, status: number) =>
  new Response(body, { status, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });

export const GET: APIRoute = async ({ params, locals }) => {
  if (!holderSignedIn(locals)) return text('Sign in first.', 401);
  const id = params.id ?? '';
  // A request id starts with a letter or digit, so `.` and `..` can never reach the backend path.
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(id)) return text('Unknown request.', 404);
  if (!ALIBI_API_ORIGIN) return text('The backend is not connected, so there is no package.', 503);

  let upstream: Response;
  try {
    upstream = await fetch(
      new URL(`/requests/${encodeURIComponent(id)}/export.json`, ALIBI_API_ORIGIN),
      { headers: backendHeaders(), signal: AbortSignal.timeout(8000) },
    );
  } catch {
    return text('The backend did not answer.', 502);
  }
  if (upstream.status === 404)
    return text('No signed package has been sent for this request.', 404);
  if (!upstream.ok) return text(`The backend answered ${upstream.status}.`, 502);
  if (!(upstream.headers.get('content-type') ?? '').includes('json'))
    return text('The backend did not answer with a package.', 502);

  return new Response(await upstream.arrayBuffer(), {
    headers: {
      'Content-Type': 'application/json',
      'Content-Disposition': `attachment; filename="alibi-package-${id}.json"`,
      'Cache-Control': 'private, no-store',
    },
  });
};
