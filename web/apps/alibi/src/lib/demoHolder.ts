/**
 * Demo holder mode: the local live demo signs in as Maya. `pnpm demo` turns it on
 * (ALIBI_DEMO_HOLDER=1 plus a fresh HOLDER_SESSION_SECRET) and binds the dev server to 127.0.0.1.
 * The middleware then refuses any non-loopback host, and every holder route needs a session: an
 * HMAC-SHA256-signed cookie `<holder>.<expiry>.<sig>`, issued by POST /demo-session/ (the bank
 * console's scheme). Deployed builds never set these variables; they run the simulation.
 */

export const HOLDER_COOKIE = 'alibi_demo_holder';
export const HOLDER_TTL_MS = 8 * 60 * 60 * 1000;
/** The only holder the demo signs in as (case 1). */
export const DEMO_HOLDER = 'maya';
/** User id of the signed-in demo holder. */
export const DEMO_USER_ID = 'demo_maya';

/** On only when both variables are set, so a stray flag without a secret cannot open anything. */
export function demoHolderEnabled(flag: string | undefined, secret: string | undefined): boolean {
  return flag === '1' && typeof secret === 'string' && secret.length >= 32;
}

/** Hostnames that are this machine. URL.hostname gives IPv6 in brackets. */
export function isLoopback(hostname: string): boolean {
  const h = hostname.toLowerCase();
  return (
    h === 'localhost' ||
    h === '[::1]' ||
    h === '::1' ||
    /^127(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$/.test(h)
  );
}

/** Routes that show or change the holder's passport. */
export function isHolderRoute(pathname: string): boolean {
  return (
    pathname === '/app' ||
    pathname.startsWith('/app/') ||
    pathname === '/phone' ||
    pathname.startsWith('/phone/') ||
    pathname === '/connect' ||
    pathname.startsWith('/connect/')
  );
}

/** A same-site path to return to after sign-in; anything else becomes /app/. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.includes('\\'))
    return '/app/';
  return next;
}

const enc = new TextEncoder();
const HOLDER_RE = /^[a-z0-9_-]{1,32}$/;

async function hmacHex(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(message)));
  return Array.from(sig, (b) => b.toString(16).padStart(2, '0')).join('');
}

function safeEqual(a: string, b: string): boolean {
  const x = enc.encode(a);
  const y = enc.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

export async function makeHolderSession(
  secret: string,
  holder: string,
  nowMs: number,
): Promise<string> {
  if (!secret || !HOLDER_RE.test(holder)) throw new Error('cannot issue session');
  const payload = `${holder}.${nowMs + HOLDER_TTL_MS}`;
  return `${payload}.${await hmacHex(secret, payload)}`;
}

/** The holder id if the token is genuine and unexpired, else null. */
export async function verifyHolderSession(
  secret: string | undefined,
  token: string | undefined,
  nowMs: number,
): Promise<string | null> {
  if (!secret || !token) return null;
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const [holder, exp, sig] = parts as [string, string, string];
  if (!HOLDER_RE.test(holder)) return null;
  const expiry = Number(exp);
  if (!Number.isFinite(expiry) || expiry <= nowMs) return null;
  return safeEqual(sig, await hmacHex(secret, `${holder}.${exp}`)) ? holder : null;
}
