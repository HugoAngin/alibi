/**
 * `/app/account/export.json`: Maya's passport data as the backend serves it
 * (`GET /passport`): connections, transactions, evidence status, alerts,
 * rules, requests and sharing history, without the page chrome. Unsigned; the
 * signed packages are downloaded one by one from her evidence passport. Live
 * data only: with no backend there is nothing of hers to export.
 */
import type { APIRoute } from 'astro';

import { isSimulated } from '@alibi/ui/sim/context';

import { holderSignedIn } from '@/components/app/home';
import { loadPassport } from '@/lib/passport';

export const prerender = false;

const KEEP = [
  'holder',
  'connections',
  'transactions',
  'evidence_status',
  'alerts',
  'rules',
  'requests',
  'sharing_history',
] as const;

const text = (body: string, status: number) =>
  new Response(body, { status, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });

export const GET: APIRoute = async ({ locals }) => {
  if (!holderSignedIn(locals, isSimulated())) return text('Sign in first.', 401);
  const loaded = await loadPassport();
  if (loaded.source !== 'live') {
    return text(`Nothing to export: ${loaded.reason}`, 503);
  }
  const data = loaded.data as unknown as Record<string, unknown>;
  const passport = Object.fromEntries(KEEP.map((k) => [k, data[k]]));
  const ref = loaded.data.holder.subject_ref.replace(/[^A-Za-z0-9._-]/g, '_');
  const body = { format: 'alibi-passport-export', signed: false, passport };
  return new Response(JSON.stringify(body, null, 2), {
    headers: {
      'Content-Type': 'application/json',
      'Content-Disposition': `attachment; filename="alibi-passport-${ref}.json"`,
      'Cache-Control': 'private, no-store',
    },
  });
};
