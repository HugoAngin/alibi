/**
 * What the console pages share: the backend routes for each screen (alibi/app/UI.md, through the
 * public router under /bank/...), the bank's credential, shape checks, and the one-shot flash that
 * carries an action's outcome back to the screen.
 */

import type { AstroCookies } from 'astro';

import type { AnalystInbox, AnalystPackage, Refusal } from './types';

/** This console is Bank A. Scope comes from the credential on the backend, never from a field. */
export const RECIPIENT = 'bank_a';

/**
 * The bank credential, read server-side from a Worker secret and forwarded as a bearer token. Its
 * absence is fine: screens fall back to the built-in case and actions report "not connected".
 */
export function backendHeaders(token: string | undefined): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}

export function isInbox(v: unknown): v is AnalystInbox {
  return (
    isObject(v) &&
    isObject(v.recipient) &&
    Array.isArray(v.packages) &&
    Array.isArray(v.requests_sent)
  );
}

export function isPackage(v: unknown): v is AnalystPackage {
  if (!isObject(v)) return false;
  const auth = v.authentication;
  return (
    isObject(v.package) &&
    isObject(auth) &&
    typeof auth.status === 'string' &&
    Array.isArray(v.source_trust) &&
    Array.isArray(v.claim_status) &&
    Array.isArray(v.evidence_by_question) &&
    isObject(v.disposition_form) &&
    isObject(v.followup_form)
  );
}

export type Flash =
  | { kind: 'ok'; message: string }
  | { kind: 'refused'; refusal: Refusal }
  | { kind: 'unavailable'; message: string };

const FLASH_COOKIE = 'bank_flash';

export function setFlash(cookies: AstroCookies, flash: Flash): void {
  cookies.set(FLASH_COOKIE, JSON.stringify(flash), {
    path: '/',
    maxAge: 60,
    httpOnly: true,
    sameSite: 'lax',
  });
}

export function takeFlash(cookies: AstroCookies): Flash | null {
  const raw = cookies.get(FLASH_COOKIE)?.value;
  if (!raw) return null;
  cookies.delete(FLASH_COOKIE, { path: '/' });
  try {
    return JSON.parse(raw) as Flash;
  } catch {
    return null;
  }
}

/** Only return to console pages (under /bank/). */
export function safeReturn(path: string | null): string {
  return path && /^\/bank\/[A-Za-z0-9._~\-/#]*$/.test(path) ? path : '/bank/inbox/';
}
