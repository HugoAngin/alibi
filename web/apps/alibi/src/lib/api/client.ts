/**
 * Server-side client for the Python backend (web/docs/backend-integration.md).
 * Runs in the Worker or dev server, never the browser, so the backend needs no
 * CORS. Pure apart from the injected `fetch`, so it is unit tested with a fake.
 *
 * Screens: `loadContext` asks for a route's context as JSON. If the backend is
 * not configured, unreachable, or answers with something that is not the
 * expected shape, it falls back to the built-in case 1 fixture and says why,
 * so the screen can show that it is not live.
 *
 * Actions: `forwardAction` posts to the action URL the context itself gave.
 * A refusal is the backend's `{code, reason}` (shared/CONTRACTS.md, Refusals),
 * passed through unchanged. Being unable to reach the backend is reported
 * separately and is never dressed up as a refusal.
 */

import type { PassportHome, Refusal, RequestDetail } from './types';

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

  if (!opts.origin) return fallback('The backend is not configured (ALIBI_API_ORIGIN).');

  const fetchImpl = opts.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await fetchImpl(new URL(opts.path, opts.origin), {
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
}

/** True for a same-backend path such as `/requests/x/approve`; rejects URLs and `//host`. */
export function isBackendPath(path: string): boolean {
  return /^\/(?!\/)[A-Za-z0-9._~\-/]*$/.test(path);
}

export async function forwardAction(opts: ActionOptions): Promise<ActionResult> {
  if (!opts.origin)
    return { kind: 'unavailable', message: 'The backend is not connected, so nothing was sent.' };
  if (!isBackendPath(opts.path))
    return { kind: 'unavailable', message: 'That action is not a backend path.' };

  const fetchImpl = opts.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await fetchImpl(new URL(opts.path, opts.origin), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...opts.headers },
      body: JSON.stringify(opts.body),
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

// Minimal shape checks: enough to know the route returned the screen's context
// and not something else. Full shapes are pinned by the TypeScript types.

export function isPassportHome(v: unknown): v is PassportHome {
  const o = v as Record<string, unknown>;
  return (
    typeof o === 'object' &&
    o !== null &&
    typeof o.holder === 'object' &&
    Array.isArray(o.transactions) &&
    Array.isArray(o.alerts) &&
    Array.isArray(o.requests)
  );
}

export function isRequestDetail(v: unknown): v is RequestDetail {
  const o = v as Record<string, unknown>;
  return (
    typeof o === 'object' &&
    o !== null &&
    typeof o.request === 'object' &&
    Array.isArray(o.trace) &&
    typeof o.review === 'object' &&
    o.review !== null &&
    typeof (o.review as Record<string, unknown>).approval === 'object'
  );
}
