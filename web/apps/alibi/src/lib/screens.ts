/**
 * What the app pages share: where the backend is, which backend route serves
 * each screen, and the one-shot "flash" that carries an action's outcome from
 * the action endpoint back to the screen.
 */

import type { AstroCookies } from 'astro';

import type { DispatchResult, Refusal } from './api/types';

/** Backend routes (docs/FRONTEND.md). Each serves its context as JSON on `Accept: application/json`. */
export const ROUTES = {
  passport: '/passport',
  request: (id: string) => `/requests/${encodeURIComponent(id)}`,
};

/**
 * Credentials to forward to the backend. The holder session scheme is not
 * published yet (web/docs/backend-integration.md, Identity); when it is, this
 * is the one place that adds it.
 */
export function backendHeaders(): Record<string, string> {
  return {};
}

export type Flash =
  | { kind: 'ok'; message: string }
  | { kind: 'dispatch'; dispatch: DispatchResult }
  | { kind: 'refused'; refusal: Refusal }
  | { kind: 'unavailable'; message: string };

const FLASH_COOKIE = 'alibi_flash';

export function setFlash(cookies: AstroCookies, flash: Flash): void {
  cookies.set(FLASH_COOKIE, JSON.stringify(flash), {
    path: '/app',
    maxAge: 60,
    httpOnly: true,
    sameSite: 'lax',
  });
}

/** Reads and clears the flash, so it shows once. */
export function takeFlash(cookies: AstroCookies): Flash | null {
  const raw = cookies.get(FLASH_COOKIE)?.value;
  if (!raw) return null;
  cookies.delete(FLASH_COOKIE, { path: '/app' });
  try {
    return JSON.parse(raw) as Flash;
  } catch {
    return null;
  }
}

/** Only return to pages inside the app. */
export function safeReturn(path: string | null): string {
  return path && /^\/app\/[A-Za-z0-9._~\-/#]*$/.test(path) ? path : '/app/';
}
