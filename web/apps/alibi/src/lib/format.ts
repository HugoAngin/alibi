/**
 * Formatting for the holder's screens. Money arrives as integer minor units
 * and times as UTC ISO strings; neither is ever recomputed here.
 */

import type { Money } from './api/types';

/** `{minor: 216000, currency: 'GBP'}` → `£2,160.00`; negative amounts get a leading minus. */
export function money({ minor, currency }: Money): string {
  return new Intl.NumberFormat('en-GB', { style: 'currency', currency }).format(minor / 100);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * `2026-10-28T06:30:00Z` → `28 Oct 2026, 06:30 UTC`. Reads the string rather
 * than going through Date, so it cannot shift with the server's time zone.
 * Anything that is not a UTC timestamp is returned unchanged.
 */
export function when(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?Z$/.exec(iso);
  if (!m) return iso;
  const [, y, mo, d, h, mi] = m;
  return `${Number(d)} ${MONTHS[Number(mo) - 1]} ${y}, ${h}:${mi} UTC`;
}

/** `2026-10-28T06:30:00Z` → `28 Oct`. */
export function day(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T/.exec(iso);
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]}` : iso;
}

/** `rec-0001-021`, 1 → `rec-0001-021 r1`. */
export function ref(recordId: string, revision: number): string {
  return `${recordId} r${revision}`;
}
