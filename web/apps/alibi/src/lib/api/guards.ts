/**
 * Minimal shape checks for the holder screens' JSON: enough to know a route
 * returned the screen's context and not something else. Full shapes are
 * pinned by the TypeScript types in ./types.ts.
 */

import type { PassportHome, RequestDetail } from './types';

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
