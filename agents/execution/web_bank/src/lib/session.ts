/**
 * Analyst session for the bank console: an HMAC-SHA256-signed cookie value `<analyst>.<expiry>.<sig>`
 * issued after the analyst passcode check (/session/). Uses WebCrypto, so it runs on the Worker and
 * in tests. Without BANK_SESSION_SECRET no session can be issued or accepted, so live actions are
 * impossible rather than open.
 */

export const SESSION_COOKIE = 'bank_session';
export const SESSION_TTL_MS = 8 * 60 * 60 * 1000;

const enc = new TextEncoder();

async function hmacHex(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, [
    'sign',
  ]);
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(message)));
  return Array.from(sig, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Constant-time comparison of two strings. */
export function safeEqual(a: string, b: string): boolean {
  const x = enc.encode(a);
  const y = enc.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

const ANALYST_RE = /^[A-Za-z0-9 ._-]{1,64}$/;

export async function makeSession(secret: string, analyst: string, nowMs: number): Promise<string> {
  if (!secret || !ANALYST_RE.test(analyst)) throw new Error('cannot issue session');
  // encodeURIComponent leaves '.', the token separator, unescaped ('J. Okafor'), so escape it too.
  const payload = `${encodeURIComponent(analyst).replaceAll('.', '%2E')}.${nowMs + SESSION_TTL_MS}`;
  return `${payload}.${await hmacHex(secret, payload)}`;
}

/** The analyst name if the token is genuine and unexpired, else null. */
export async function verifySession(
  secret: string | undefined,
  token: string | undefined,
  nowMs: number,
): Promise<string | null> {
  if (!secret || !token) return null;
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const [name, exp, sig] = parts as [string, string, string];
  const expiry = Number(exp);
  if (!Number.isFinite(expiry) || expiry <= nowMs) return null;
  const expected = await hmacHex(secret, `${name}.${exp}`);
  if (!safeEqual(sig, expected)) return null;
  try {
    const analyst = decodeURIComponent(name);
    return ANALYST_RE.test(analyst) ? analyst : null;
  } catch {
    return null;
  }
}
