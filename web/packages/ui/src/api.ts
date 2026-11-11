/**
 * Server-side client for the Python backend (web/docs/backend-integration.md),
 * shared by the Alibi site and the bank console. On a simulated site (ALIBI_SIMULATION=1) the
 * same calls are answered from the recorded demo (src/sim/), with no network.
 * Runs in the Worker or dev server, never the browser, so the backend needs no
 * CORS. Pure apart from the injected `fetch`, so it is unit tested with a fake.
 *
 * Screens: `loadContext` asks for a route's context as JSON. If the backend is
 * not configured, unreachable, or answers with something that is not the
 * expected shape, it falls back to the site's built-in case 1 fixture and says why,
 * so the screen can show that it is not live.
 *
 * Actions: `forwardAction` posts to the action URL the context itself gave.
 * A refusal is the backend's `{code, reason}` (shared/CONTRACTS.md, Refusals),
 * passed through unchanged. Being unable to reach the backend is reported
 * separately and is never dressed up as a refusal.
 */

import { SIM_ORIGIN } from './sim/backend';
import { currentSimulation } from './sim/context';

/** A backend refusal (shared/CONTRACTS.md, Refusals): shown verbatim, never reworded. */
export interface Refusal {
  code: string;
  reason: string;
}

export type Loaded<T> =
  | { source: 'live'; data: T }
  | { source: 'fixture'; data: T; reason: string }
  | { source: 'missing'; reason: string };

interface LoadOptions<T> {
  /** Backend origin, e.g. `http://127.0.0.1:8100`. Empty or undefined means not configured. */
  origin: string | undefined;
  /** Backend route, e.g. `/passport` or `/requests/rfc-0001-01`. */
  path: string;
  /** Minimal shape check for the JSON the route returns. */
  isShape: (value: unknown) => value is T;
  /** Fallback data for this route, or null when the fixture has no such item. */
  fixture: () => T | null;
  /** Credentials and other headers to forward (holder session). */
  headers?: Record<string, string>;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export async function loadContext<T>(opts: LoadOptions<T>): Promise<Loaded<T>> {
  const fallback = (reason: string): Loaded<T> => {
    const data = opts.fixture();
    return data ? { source: 'fixture', data, reason } : { source: 'missing', reason };
  };

  // A simulated site (src/sim/) answers from the recorded demo, whatever origin is configured.
  const sim = currentSimulation();
  const origin = sim ? SIM_ORIGIN : opts.origin;
  if (!origin) return fallback('The backend is not configured (ALIBI_API_ORIGIN).');

  const fetchImpl = opts.fetchImpl ?? sim?.fetch ?? fetch;
  let response: Response;
  try {
    response = await fetchImpl(new URL(opts.path, origin), {
      headers: { Accept: 'application/json', ...opts.headers },
      signal: AbortSignal.timeout(opts.timeoutMs ?? 4000),
    });
  } catch {
    return fallback('The backend did not answer.');
  }

  if (response.status === 404)
    return { source: 'missing', reason: 'The backend has no such item.' };
  if (!response.ok) return fallback(`The backend answered ${response.status}.`);
  if (!(response.headers.get('content-type') ?? '').includes('application/json')) {
    return fallback(
      'The backend answered with a page, not JSON. Its JSON routes may not be merged yet.',
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return fallback('The backend answered with malformed JSON.');
  }
  if (!opts.isShape(body))
    return fallback('The backend’s JSON does not match the screen contract.');
  return { source: 'live', data: body };
}

export type ActionResult =
  | { kind: 'ok'; data: unknown }
  | { kind: 'refused'; refusal: Refusal }
  | { kind: 'unavailable'; message: string };

interface ActionOptions {
  origin: string | undefined;
  /** The action path exactly as the screen context gave it. */
  path: string;
  body: Record<string, string>;
  headers?: Record<string, string>;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  encoding?: 'form';
}

/** True for a same-backend path such as `/requests/x/approve`; rejects URLs and `//host`. */
export function isBackendPath(path: string): boolean {
  return /^\/(?!\/)[A-Za-z0-9._~\-/]*$/.test(path);
}

export async function forwardAction(opts: ActionOptions): Promise<ActionResult> {
  const sim = currentSimulation();
  const origin = sim ? SIM_ORIGIN : opts.origin;
  if (!origin)
    return { kind: 'unavailable', message: 'The backend is not connected, so nothing was sent.' };
  if (!isBackendPath(opts.path))
    return { kind: 'unavailable', message: 'That action is not a backend path.' };

  const fetchImpl = opts.fetchImpl ?? sim?.fetch ?? fetch;
  let response: Response;
  try {
    response = await fetchImpl(new URL(opts.path, origin), {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type':
          opts.encoding === 'form' ? 'application/x-www-form-urlencoded' : 'application/json',
        ...opts.headers,
      },
      body: opts.encoding === 'form' ? new URLSearchParams(opts.body) : JSON.stringify(opts.body),
      signal: AbortSignal.timeout(opts.timeoutMs ?? 8000),
    });
  } catch {
    return {
      kind: 'unavailable',
      message: 'The backend did not answer, so the outcome is unknown.',
    };
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (response.ok) return { kind: 'ok', data: body };
  if (response.status >= 400 && response.status < 500 && isRefusal(body))
    return { kind: 'refused', refusal: body };
  return {
    kind: 'unavailable',
    message: `The backend answered ${response.status} without a refusal reason.`,
  };
}

function isRefusal(value: unknown): value is Refusal {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as Refusal).code === 'string' &&
    typeof (value as Refusal).reason === 'string'
  );
}
