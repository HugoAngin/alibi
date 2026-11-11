/**
 * Server-side client for the demo director (alibi/demo_director.py): the live demo's state and
 * its three steps (withdraw, request, reset). Runs in the Worker or dev server only; the demo
 * bearer never reaches a browser.
 *
 * Configuration, per app: `ALIBI_DEMO_ORIGIN` (local: http://127.0.0.1:8103) and
 * `ALIBI_DEMO_TOKEN`. Unset means no live demo: every screen keeps working exactly as before. On a
 * simulated site (ALIBI_SIMULATION=1) the demo is always on and answered from the recording
 * (src/sim/), with no director and no credential.
 */

import { forwardAction, type ActionResult } from './api';
import { SIM_ORIGIN } from './sim/backend';
import { currentSimulation } from './sim/context';

export type DemoStage = 'ready' | 'withdrawn' | 'flagged' | 'requested' | 'received';

export interface DemoState {
  /** Changes whenever anything a screen shows changes; the sites re-render on a new value. */
  version: string;
  stage: DemoStage;
  /** The shared simulated clock. */
  as_of: string | null;
  steps: { withdrawn: boolean; flagged: boolean; requested: boolean; received: boolean };
  request_id: string;
  package_id: string | null;
  credit_txn: string;
  flagged_at: string;
}

export interface DemoTarget {
  origin: string | undefined;
  token: string | undefined;
}

export const demoEnabled = (t: DemoTarget): boolean =>
  currentSimulation() !== null || Boolean(t.origin && t.token);

export function isDemoState(v: unknown): v is DemoState {
  if (typeof v !== 'object' || v === null) return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.version === 'string' &&
    typeof o.stage === 'string' &&
    typeof o.steps === 'object' &&
    o.steps !== null
  );
}

const auth = (t: DemoTarget) => ({ Authorization: `Bearer ${t.token ?? ''}` });

/** The director's state, or null when the demo is not configured or not answering. */
export async function demoState(
  t: DemoTarget,
  fetchImpl: typeof fetch = fetch,
): Promise<DemoState | null> {
  if (!demoEnabled(t)) return null;
  const sim = currentSimulation();
  try {
    const r = await (sim?.fetch ?? fetchImpl)(new URL('/demo/state', sim ? SIM_ORIGIN : t.origin), {
      headers: { Accept: 'application/json', ...auth(t) },
      signal: AbortSignal.timeout(3000),
    });
    if (!r.ok) return null;
    const body: unknown = await r.json();
    return isDemoState(body) ? body : null;
  } catch {
    return null;
  }
}

export type DemoStep = 'withdraw' | 'request' | 'reset';

/** Run one step. Refusals come back as the director's `{code, reason}`, word for word. */
export function demoStep(
  t: DemoTarget,
  step: DemoStep,
  body: Record<string, string> = {},
  fetchImpl?: typeof fetch,
): Promise<ActionResult> {
  if (!demoEnabled(t))
    return Promise.resolve({
      kind: 'unavailable',
      message: 'The live demo is not connected here, so nothing changed.',
    });
  return forwardAction({
    origin: t.origin,
    path: `/demo/${step}`,
    body,
    headers: auth(t),
    fetchImpl,
    timeoutMs: 20000,
  });
}

/** The JSON a site's `/live/` endpoint answers: only what the browser needs to poll. */
export function liveBody(s: DemoState | null): { version: string | null; stage: DemoStage | null } {
  return { version: s?.version ?? null, stage: s?.stage ?? null };
}
