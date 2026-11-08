/**
 * Minimal shape checks for the holder screens' JSON: enough to know a route
 * returned the screen's context and not something else. Full shapes are
 * pinned by the TypeScript types in ./types.ts.
 */

import type { DispatchResult, PassportHome, RequestDetail } from './types';

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

/** Accepts the dispatch context bare or wrapped as `{dispatch: ...}`. */
export function asDispatch(v: unknown): DispatchResult | null {
  const o = (v as { dispatch?: unknown })?.dispatch ?? v;
  const d = o as Record<string, unknown>;
  return typeof d === 'object' &&
    d !== null &&
    (d.outcome === 'success' || d.outcome === 'refused') &&
    typeof d.request_id === 'string'
    ? (d as unknown as DispatchResult)
    : null;
}
