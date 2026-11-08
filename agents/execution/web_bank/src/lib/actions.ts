/**
 * The bank console's only two actions, mapped to Bank A routes on the server. The form never
 * supplies a backend path: it names a fixed kind and an ID, both validated here, and only the
 * allowlisted fields are forwarded (eval, 04:17Z: /act/ must not forward arbitrary paths with the
 * Bank A credential). `handleAct` makes no upstream call unless the request is same-origin, carries
 * a valid analyst session, and plans to a known action.
 */

import type { ActionResult } from '@alibi/ui/api';

import { BANK_ROUTES, type BankTarget } from './bankApi';

export type ActKind = 'decision' | 'followup';

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DECISIONS = new Set(['clear', 'escalate', 'request_more']);
/** The bank service bounds `reason` at 300 characters (REASON_MAX) and evidence text at 1000. */
const MAX_REASON = 300;
const MAX_TEXT = 1000;

/** Body values may be string lists: the bank wants `requested_evidence` as a list of strings. */
export type Body = Record<string, string | string[]>;

export type Plan = { ok: true; path: string; body: Body } | { ok: false; reason: string };

function text(fields: Record<string, string>, key: string, max: number): string | null {
  const v = (fields[key] ?? '').trim();
  return v.length >= 3 && v.length <= max ? v : null;
}

/**
 * Fixed kind + validated ID -> server-built bank path and allowlisted body, in the bank service's
 * format: a decision is `{decision, reason}`; a follow-up is `request_more` on the package, with
 * the question as `requested_evidence` (the bank narrows its scope from the parent request).
 */
export function planAction(fields: Record<string, string>, target: BankTarget = { origin: undefined, prefix: '' }): Plan {
  const kind = fields.kind;
  if (kind === 'decision') {
    const id = fields.package_id ?? '';
    if (!ID_RE.test(id)) return { ok: false, reason: 'invalid package id' };
    const disposition = fields.disposition ?? '';
    if (!DECISIONS.has(disposition)) return { ok: false, reason: 'invalid decision' };
    const reason = text(fields, 'note', MAX_REASON);
    if (!reason) return { ok: false, reason: `a reason of 3 to ${MAX_REASON} characters is required` };
    return { ok: true, path: BANK_ROUTES.disposition(target, id), body: { decision: disposition, reason } };
  }
  if (kind === 'followup') {
    const id = fields.package_id ?? '';
    if (!ID_RE.test(id)) return { ok: false, reason: 'invalid package id' };
    const question = text(fields, 'question', MAX_TEXT);
    if (!question) return { ok: false, reason: `a question of 3 to ${MAX_TEXT} characters is required` };
    const reason = `Follow-up requested: ${question}`.slice(0, MAX_REASON);
    return {
      ok: true,
      path: BANK_ROUTES.disposition(target, id),
      body: { decision: 'request_more', reason, requested_evidence: [question] },
    };
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
  body: Body;
  headers: Record<string, string>;
}) => Promise<ActionResult>;

export interface ActDeps {
  target: BankTarget;
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
  const plan = planAction(input.fields, deps.target);
  if (!plan.ok) return { kind: 'forbidden', status: 400, message: `Not sent: ${plan.reason}.` };
  const result = await deps.forward({ origin: deps.target.origin, path: plan.path, body: plan.body, headers: deps.headers });
  return { kind: 'sent', result };
}
