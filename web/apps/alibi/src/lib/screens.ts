/**
 * What the app pages share: where the backend is, which backend route serves
 * each screen, and the one-shot "flash" that carries an action's outcome from
 * the action endpoint back to the screen.
 */

import type { AstroCookies } from 'astro';
import { ALIBI_HOLDER_TOKEN } from 'astro:env/server';

import type { DispatchResult, Refusal } from './api/types';

/** Backend routes (docs/FRONTEND.md). Each serves its context as JSON on `Accept: application/json`. */
export const ROUTES = {
  passport: '/passport',
  request: (id: string) => `/requests/${encodeURIComponent(id)}`,
  /** One bank transaction's detail context: linked sources, claims, reconciliation. */
  txn: (id: string) => `/txn/${encodeURIComponent(id)}`,
  /** The signed package sent for a request, as the backend stored it. */
  exportJson: (id: string) => `/requests/${encodeURIComponent(id)}/export.json`,
};

/**
 * Credentials to forward to the backend: the holder bearer (`ALIBI_HOLDER_TOKEN`, alibi/app/UI.md
 * Credentials), read on the server only and never sent to the browser. Unset (the local demo),
 * the backend's holder routes ask for nothing. Holder pages and actions sit behind sign-in
 * (src/middleware.ts); `/simulate/` uses it only for reads, since it renders no action.
 */
export function backendHeaders(): Record<string, string> {
  return ALIBI_HOLDER_TOKEN ? { Authorization: `Bearer ${ALIBI_HOLDER_TOKEN}` } : {};
}

export type Flash =
  | { kind: 'ok'; message: string }
  | { kind: 'dispatch'; dispatch: DispatchResult }
  | { kind: 'refused'; refusal: Refusal }
  | { kind: 'unavailable'; message: string };

const FLASH_COOKIE = 'alibi_flash';
// Site-wide: Maya's screens live under /app/ and, in the live demo, under /simulate/ too.
const FLASH_PATH = '/';

export function setFlash(cookies: AstroCookies, flash: Flash): void {
  cookies.set(FLASH_COOKIE, JSON.stringify(flash), {
    path: FLASH_PATH,
    maxAge: 60,
    httpOnly: true,
    sameSite: 'lax',
  });
}

/** Reads and clears the flash, so it shows once. */
export function takeFlash(cookies: AstroCookies): Flash | null {
  const raw = cookies.get(FLASH_COOKIE)?.value;
  if (!raw) return null;
  cookies.delete(FLASH_COOKIE, { path: FLASH_PATH });
  try {
    return JSON.parse(raw) as Flash;
  } catch {
    return null;
  }
}
