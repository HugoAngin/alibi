/**
 * Trusted banks: banks the holder chose to trust from their connection page. A trusted bank's
 * requests arrive marked "Trusted", and the request page puts one "Approve and send" button at the
 * top. The holder still taps once for every disclosure (alibi/SPEC.md: every demo disclosure
 * requires individual approval), and the backend still binds that approval to the exact package
 * digest; trust only removes steps, never consent.
 *
 * Stored per browser in a cookie of bank ids (`bank_a,bank_b`); the backend has no field for it.
 */

export const TRUST_COOKIE = 'alibi_trusted';
const AUDIENCE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

export function isAudience(value: string): boolean {
  return AUDIENCE.test(value);
}

/** The trusted bank ids in a cookie value; anything malformed is ignored. */
export function parseTrusted(value: string | undefined | null): Set<string> {
  return new Set((value ?? '').split(',').filter(isAudience));
}

/** The cookie value after trusting or untrusting one bank. */
export function withTrust(current: Set<string>, audience: string, trusted: boolean): string {
  const next = new Set(current);
  if (!isAudience(audience)) return [...next].sort().join(',');
  if (trusted) next.add(audience);
  else next.delete(audience);
  return [...next].sort().join(',');
}
