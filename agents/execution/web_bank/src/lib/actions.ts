/**
 * The bank console's only two actions, mapped to Bank A routes on the server. The form never
 * supplies a backend path: it names a fixed kind and an ID, both validated here, and only the
 * allowlisted fields are forwarded (eval, 04:17Z: /act/ must not forward arbitrary paths with the
 * Bank A credential). `handleAct` makes no upstream call unless the request is same-origin, carries
 * a valid analyst session, and plans to a known action.
 */

import type { ActionResult } from '@alibi/ui/api';

import { RECIPIENT } from './screens';

export type ActKind = 'decision' | 'followup';

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DECISIONS = new Set(['clear', 'escalate', 'request_more']);
const MAX_TEXT = 1000;

export type Plan = { ok: true; path: string; body: Record<string, string> } | { ok: false; reason: string };

function text(fields: Record<string, string>, key: string): string | null {
  const v = (fields[key] ?? '').trim();
  return v.length >= 3 && v.length <= MAX_TEXT ? v : null;
}

/** Fixed kind + validated ID -> server-built Bank A path and allowlisted body. */
export function planAction(fields: Record<string, string>): Plan {
  const kind = fields.kind;
  if (kind === 'decision') {
    const id = fields.package_id ?? '';
    if (!ID_RE.test(id)) return { ok: false, reason: 'invalid package id' };
    const disposition = fields.disposition ?? '';
    if (!DECISIONS.has(disposition)) return { ok: false, reason: 'invalid decision' };
    const note = text(fields, 'note');
    if (!note) return { ok: false, reason: 'a reason of 3 to 1000 characters is required' };
    return {
      ok: true,
      path: `/bank/${RECIPIENT}/packages/${encodeURIComponent(id)}/disposition`,
      body: { disposition, note },
    };
  }
  if (kind === 'followup') {
    const parent = fields.parent_request_id ?? '';
    if (!ID_RE.test(parent)) return { ok: false, reason: 'invalid parent request id' };
    const question = text(fields, 'question');
    if (!question) return { ok: false, reason: 'a question of 3 to 1000 characters is required' };
    return { ok: true, path: `/bank/${RECIPIENT}/requests`, body: { parent_request_id: parent, question } };
  }
  return { ok: false, reason: 'unknown action' };
}

export interface ActInput {
  fields: Record<string, string>;
  /** Origin header of the POST, and the console's own origin. */
  requestOrigin: string | null;
  siteOrigin: string;
  /** Analyst name from a verified session, or null. */
  analyst: string | null;
}

/** The upstream call, injected: `forwardAction` from '@alibi/ui/api' in the endpoint, a recorder in tests. */
export type Forward = (opts: {
  origin: string | undefined;
  path: string;
  body: Record<string, string>;
  headers: Record<string, string>;
}) => Promise<ActionResult>;

export interface ActDeps {
  origin: string | undefined;
  headers: Record<string, string>;
  forward: Forward;
}

export type ActOutcome =
  | { kind: 'forbidden'; status: 403 | 401 | 400; message: string }
  | { kind: 'sent'; result: ActionResult };

export async function handleAct(input: ActInput, deps: ActDeps): Promise<ActOutcome> {
  if (input.requestOrigin !== input.siteOrigin)
    return { kind: 'forbidden', status: 403, message: 'Cross-site request refused.' };
  if (!input.analyst) return { kind: 'forbidden', status: 401, message: 'Sign in as an analyst first.' };
  const plan = planAction(input.fields);
  if (!plan.ok) return { kind: 'forbidden', status: 400, message: `Not sent: ${plan.reason}.` };
  const result = await deps.forward({ origin: deps.origin, path: plan.path, body: plan.body, headers: deps.headers });
  return { kind: 'sent', result };
}
